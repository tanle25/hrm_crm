from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.logging import get_logger
from app.postgres import get_connection, serialize_json
from Flowkit.flowkit_client import CharacterInput, FlowKitClient, SceneInput, SceneResult, VideoResult


settings = get_settings()
router = APIRouter(prefix=f"{settings.api_prefix}/flowkit", tags=["flowkit"])
log = get_logger("content_forge.flowkit")
LOCAL_JOB_DIR = Path("data/flowkit_jobs")


def _normalize_flowkit_material(value: str) -> str:
    normalized = str(value or "realistic").strip().lower()
    aliases = {
        "cinematic": "realistic",
        "product_ad": "realistic",
        "product-ad": "realistic",
        "watercolor": "oil_painting",
        "water_color": "oil_painting",
        "pixar": "3d_pixar",
        "3d": "3d_pixar",
    }
    allowed = {"realistic", "3d_pixar", "anime", "stop_motion", "minecraft", "oil_painting"}
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in allowed else "realistic"


class FlowKitCharacterRequest(BaseModel):
    name: str
    description: str = ""
    entity_type: str = "character"
    voice_description: Optional[str] = None


class FlowKitSceneRequest(BaseModel):
    prompt: str
    video_prompt: str = ""
    image_prompt: Optional[str] = None
    chain_type: str = "ROOT"
    character_names: Optional[list[str]] = None
    transition_prompt: Optional[str] = None
    narrator_text: Optional[str] = None
    upload_image_path: Optional[str] = None
    upload_image_media_id: Optional[str] = None


class FlowKitGenerateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    scenes: list[FlowKitSceneRequest] = Field(default_factory=list)
    project_id: Optional[str] = None
    description: str = ""
    story: str = ""
    material: str = "realistic"
    language: str = "en"
    orientation: str = "HORIZONTAL"
    video_gen_mode: str = "i2v"
    upscale_4k: bool = False
    allow_music: bool = False
    allow_voice: bool = False
    characters: Optional[list[FlowKitCharacterRequest]] = None
    generate_refs: bool = True
    output_count: int = Field(default=1, ge=1, le=4)
    model: str = "default"


class FlowKitQuickGenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    video_prompt: str = ""
    orientation: str = "HORIZONTAL"
    upscale_4k: bool = False
    material: str = "realistic"


def _now() -> str:
    return datetime.utcnow().isoformat()


def _client() -> FlowKitClient:
    return FlowKitClient(
        base_url=settings.flowkit_base_url,
        api_key=settings.flowkit_api_key,
        poll_interval=settings.flowkit_poll_interval_sec,
        image_timeout=settings.flowkit_image_timeout_sec,
        video_timeout=settings.flowkit_video_timeout_sec,
        upscale_timeout=settings.flowkit_upscale_timeout_sec,
    )


def _job_path(job_id: str) -> Path:
    return LOCAL_JOB_DIR / f"{job_id}.json"


def _upsert_job(job: dict[str, Any]) -> None:
    job["updated_at"] = _now()
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO flowkit_jobs (job_id, status, updated_at, data)
                VALUES (%s, %s, NOW(), %s::jsonb)
                ON CONFLICT (job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (job["job_id"], job.get("status", "processing"), serialize_json(job)),
            )
        return
    LOCAL_JOB_DIR.mkdir(parents=True, exist_ok=True)
    _job_path(job["job_id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_job(job_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM flowkit_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
    path = _job_path(job_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _list_jobs(limit: int = 50, status: str = "") -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT data::text FROM flowkit_jobs WHERE status = %s ORDER BY updated_at DESC LIMIT %s",
                    (status, limit),
                )
            else:
                cur.execute("SELECT data::text FROM flowkit_jobs ORDER BY updated_at DESC LIMIT %s", (limit,))
            return [json.loads(row[0]) for row in cur.fetchall()]
    if not LOCAL_JOB_DIR.exists():
        return []
    jobs = [json.loads(path.read_text(encoding="utf-8")) for path in LOCAL_JOB_DIR.glob("*.json")]
    if status:
        jobs = [job for job in jobs if job.get("status") == status]
    return sorted(jobs, key=lambda item: item.get("updated_at", ""), reverse=True)[:limit]


def _result_payload(result: VideoResult) -> dict[str, Any]:
    return {
        "project_id": result.project_id,
        "video_id": result.video_id,
        "concat_url": result.concat_url,
        "status": result.status,
        "error": result.error,
        "characters": result.characters,
        "scenes": [
            {
                "id": scene.id,
                "prompt": scene.prompt,
                "image_url": scene.image_url,
                "image_media_id": scene.image_media_id,
                "video_url": scene.video_url,
                "video_media_id": scene.video_media_id,
                "upscale_url": scene.upscale_url,
                "status": scene.status,
                "error": scene.error,
            }
            for scene in result.scenes
        ],
    }


def _proxied_media_url(url: str) -> str:
    if not url:
        return ""
    return f"{settings.api_prefix}/flowkit/media?url={quote(url, safe='')}"


def _update_progress(job_id: str, stage: str, detail: str) -> None:
    job = _get_job(job_id)
    if not job:
        return
    progress = job.setdefault("progress", [])
    progress.append({"stage": stage, "detail": detail, "time": _now()})
    _upsert_job(job)


def _finish_job(job_id: str, result: VideoResult) -> None:
    job = _get_job(job_id)
    if not job:
        return
    job["status"] = "completed"
    job["completed_at"] = _now()
    job["result"] = _with_proxied_media(_result_payload(result))
    _upsert_job(job)


def _finish_multi_output_job(job_id: str, results: list[VideoResult]) -> None:
    job = _get_job(job_id)
    if not job:
        return
    payloads = [_result_payload(result) for result in results]
    payloads = [_with_proxied_media(payload) for payload in payloads]
    job["status"] = "completed"
    job["completed_at"] = _now()
    job["result"] = payloads[0] if payloads else None
    job["outputs"] = payloads
    _upsert_job(job)


def _fail_job(job_id: str, error: str) -> None:
    job = _get_job(job_id)
    if not job:
        return
    job["status"] = "failed"
    job["error"] = error
    job["failed_at"] = _now()
    _upsert_job(job)


def _with_proxied_media(payload: dict[str, Any]) -> dict[str, Any]:
    for scene in payload.get("scenes") or []:
        for key in ["image_url"]:
            url = str(scene.get(key) or "")
            if url:
                scene[f"{key}_proxied"] = _proxied_media_url(url)
    return payload


def _scene_inputs(scenes: list[FlowKitSceneRequest]) -> list[SceneInput]:
    return [
        SceneInput(
            prompt=scene.prompt,
            video_prompt=scene.video_prompt,
            image_prompt=scene.image_prompt,
            chain_type=scene.chain_type,
            character_names=scene.character_names,
            transition_prompt=scene.transition_prompt,
            narrator_text=scene.narrator_text,
            upload_image_path=scene.upload_image_path,
            upload_image_media_id=scene.upload_image_media_id,
        )
        for scene in scenes
    ]


def _character_inputs(characters: Optional[list[FlowKitCharacterRequest]]) -> list[CharacterInput] | None:
    if not characters:
        return None
    return [
        CharacterInput(
            name=character.name,
            description=character.description,
            entity_type=character.entity_type,
            voice_description=character.voice_description,
        )
        for character in characters
    ]


def _simple_image_prompt(prompt: str, material: str, orientation: str) -> str:
    style = _normalize_flowkit_material(material).replace("_", " ")
    frame = "vertical 9:16 portrait frame" if orientation == "VERTICAL" else "horizontal 16:9 cinematic frame"
    return (
        f"{prompt}\n\n"
        f"Create the exact first frame for a video. Style/material: {style}. "
        f"Composition: {frame}, edge-to-edge, no black bars, no text, no watermark. "
        "The image must follow the subject, setting, action, mood, and visual details from the user prompt."
    )


def _simple_video_prompt(prompt: str, orientation: str) -> str:
    frame_guard = (
        "True vertical 9:16 mobile video, fill the entire portrait frame edge to edge. "
        if orientation == "VERTICAL"
        else "True horizontal 16:9 video, fill the frame edge to edge. "
    )
    return (
        f"{frame_guard}"
        "Use the generated first frame as the exact starting frame. "
        f"0-3s: Begin from the prompt scene with subtle natural motion. "
        f"3-6s: Add cinematic camera movement and environmental motion while preserving the subject. "
        f"6-8s: Settle into a polished final shot. User prompt: {prompt}"
    )


def _variant_video_prompt(base_prompt: str, index: int, total: int, orientation: str) -> str:
    variants = [
        "Use a slow cinematic push-in with gentle subject motion.",
        "Use a subtle side dolly move with natural environmental motion.",
        "Use a soft handheld commercial shot with a polished final hold.",
        "Use a close-up reveal, then ease back into a wider final composition.",
    ]
    return (
        f"{_simple_video_prompt(base_prompt, orientation)}\n"
        f"Variant {index + 1}/{total}: {variants[index % len(variants)]} "
        "Keep the same first-frame identity and visual continuity; do not change the subject."
    )


def _scene_media(scene: dict[str, Any], orientation: str, media_type: str) -> tuple[str, str]:
    prefix = "vertical" if orientation == "VERTICAL" else "horizontal"
    media_id = str(scene.get(f"{prefix}_{media_type}_media_id") or "")
    url = str(scene.get(f"{prefix}_{media_type}_url") or "")
    if orientation != "VERTICAL":
        media_id = media_id or str(scene.get(f"{media_type}_media_id") or scene.get("media_id") or "")
        url = url or str(scene.get(f"{media_type}_url") or scene.get("output_url") or scene.get("url") or "")
    return media_id, url


def _crop_coordinates(file_path: str, orientation: str) -> dict[str, float] | None:
    if orientation != "VERTICAL":
        return None
    try:
        from PIL import Image

        with Image.open(file_path) as image:
            width, height = image.size
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    target = 9 / 16
    aspect = width / height
    if aspect > target:
        crop_width = height * target
        left = (width - crop_width) / 2 / width
        return {"top": 0, "left": round(left, 6), "bottom": 1, "right": round(1 - left, 6)}
    if aspect < target:
        crop_height = width / target
        top = (height - crop_height) / 2 / height
        return {"top": round(top, 6), "left": 0, "bottom": round(1 - top, 6), "right": 1}
    return {"top": 0, "left": 0, "bottom": 1, "right": 1}


async def _ensure_flowkit_project(job_id: str, request: FlowKitGenerateRequest) -> str:
    if request.project_id:
        return request.project_id
    _update_progress(job_id, "project", f"Creating shared project: {request.title}")
    project = await _client().create_project(
        name=request.title,
        description=request.description,
        story=request.story,
        material=_normalize_flowkit_material(request.material),
        language=request.language,
        allow_music=request.allow_music,
        allow_voice=request.allow_voice,
        characters=[
            {
                "name": character.name,
                "description": character.description,
                "entity_type": character.entity_type,
                **({"voice_description": character.voice_description} if character.voice_description else {}),
            }
            for character in (request.characters or [])
        ] or None,
    )
    project_id = str(project.get("id") or project.get("project_id") or "")
    if not project_id:
        raise RuntimeError(f"FlowKit create_project returned no project id: {project}")
    _update_progress(job_id, "project", f"Shared project created: {project_id}")
    return project_id


async def _run_batch_variant_job(job_id: str, request: FlowKitGenerateRequest, output_count: int) -> list[VideoResult]:
    if not request.scenes:
        raise RuntimeError("At least one scene is required.")
    client = _client()
    orientation = "VERTICAL" if str(request.orientation or "").upper() in {"VERTICAL", "PORTRAIT", "9:16"} else "HORIZONTAL"
    project_id = await _ensure_flowkit_project(job_id, request)
    title = request.title
    source_scene = request.scenes[0]
    _update_progress(job_id, "video", f"Creating shared video container in project {project_id}")
    video = await client.create_video_container(project_id, title, orientation=orientation)
    video_id = str(video.get("id") or "")
    if not video_id:
        raise RuntimeError(f"FlowKit create video returned no id: {video}")

    image_media_id = ""
    image_url = ""
    crop = _crop_coordinates(source_scene.upload_image_path, orientation) if source_scene.upload_image_path else None

    if source_scene.upload_image_path:
        _update_progress(job_id, "images", "Uploading one source image for all variants")
        upload = await client.upload_image_file(source_scene.upload_image_path, project_id)
        image_media_id = str(upload.get("media_id") or upload.get("id") or "")
        image_url = str(upload.get("url") or upload.get("image_url") or "")
        if not image_media_id:
            raise RuntimeError(f"FlowKit image upload returned no media id: {upload}")
    else:
        _update_progress(job_id, "images", "Generating one source image for all variants")
        seed_scene = await client.create_scene(
            video_id=video_id,
            prompt=source_scene.prompt,
            image_prompt=source_scene.image_prompt or source_scene.prompt,
            video_prompt=source_scene.video_prompt,
            display_order=output_count,
            chain_type="ROOT",
            orientation=orientation,
        )
        seed_scene_id = str(seed_scene.get("id") or "")
        image_req = await client.submit_request(
            req_type="GENERATE_IMAGE",
            scene_id=seed_scene_id,
            video_id=video_id,
            project_id=project_id,
            orientation=orientation,
        )
        await client.poll_request(
            str(image_req.get("id")),
            client.image_timeout,
            "Batch source image",
            on_progress=lambda detail: _update_progress(job_id, "images", f"Source image: {detail}"),
        )
        seed_state = await client.get_scene(seed_scene_id)
        image_media_id, image_url = _scene_media(seed_state, orientation, "image")
        if not image_media_id:
            raise RuntimeError("Source image generation completed but returned no image media id.")

    orient_prefix = "vertical" if orientation == "VERTICAL" else "horizontal"
    scenes: list[dict[str, Any]] = []
    scene_results: list[SceneResult] = []
    _update_progress(job_id, "scenes", f"Creating {output_count} variant scenes using one source image")
    for index in range(output_count):
        scene = await client.create_scene(
            video_id=video_id,
            prompt=source_scene.prompt,
            video_prompt=_variant_video_prompt(source_scene.prompt, index, output_count, orientation),
            image_prompt=source_scene.image_prompt or source_scene.prompt,
            display_order=index,
            chain_type="ROOT",
            orientation=orientation,
        )
        scene_id = str(scene.get("id") or "")
        update_payload: dict[str, Any] = {
            f"{orient_prefix}_image_media_id": image_media_id,
            f"{orient_prefix}_image_status": "COMPLETED",
        }
        if crop:
            update_payload[f"{orient_prefix}_image_crop_coordinates"] = json.dumps(crop, separators=(",", ":"))
        await client.update_scene(scene_id, **update_payload)
        scenes.append(scene)
        scene_results.append(SceneResult(id=scene_id, prompt=source_scene.prompt, image_url=image_url, image_media_id=image_media_id, status="IMAGE_READY"))

    _update_progress(job_id, "videos", f"Queueing {output_count} video variants in one batch")
    batch_requests = [
        {
            "type": "GENERATE_VIDEO",
            "orientation": orientation,
            "scene_id": str(scene.get("id") or ""),
            "project_id": project_id,
            "video_id": video_id,
        }
        for scene in scenes
    ]
    await client.submit_batch(batch_requests)
    deadline = asyncio.get_running_loop().time() + client.video_timeout
    while True:
        status = await client.get_batch_status(video_id=video_id, req_type="GENERATE_VIDEO", orientation=orientation)
        total = max(1, int(status.get("total") or output_count))
        completed = int(status.get("completed") or 0)
        failed = int(status.get("failed") or 0)
        processing = int(status.get("processing") or 0)
        percent = min(99, round(((completed + failed + processing * 0.5) / total) * 100))
        _update_progress(job_id, "videos", f"Batch video progress {percent}% ({completed}/{total} completed, {processing} processing)")
        if status.get("done"):
            if failed:
                raise RuntimeError(f"Batch video generation completed with failed requests: {status}")
            break
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"Batch video generation timeout after {client.video_timeout}s: {status}")
        await asyncio.sleep(client.poll_interval)

    scene_states = {str(scene.get("id") or ""): scene for scene in await client.list_scenes(video_id)}
    results: list[VideoResult] = []
    for index, scene_result in enumerate(scene_results):
        state = scene_states.get(scene_result.id) or await client.get_scene(scene_result.id)
        video_media_id, video_url = _scene_media(state, orientation, "video")
        if not video_url:
            raise RuntimeError(f"Variant {index + 1} completed but returned no video URL.")
        scene_result.video_media_id = video_media_id
        scene_result.video_url = video_url
        scene_result.status = "VIDEO_READY"
        result = VideoResult(project_id=project_id, video_id=video_id, status="COMPLETED")
        result.scenes.append(scene_result)
        results.append(result)
    return results


async def _run_generate_job(job_id: str, request: FlowKitGenerateRequest) -> None:
    job = _get_job(job_id)
    if job:
        job["status"] = "processing"
        _upsert_job(job)
    try:
        output_count = max(1, min(int(request.output_count or 1), 4))
        if output_count > 1 and not request.upscale_4k and not request.characters and str(request.video_gen_mode or "i2v").lower() != "r2v":
            results = await _run_batch_variant_job(job_id, request, output_count)
            _finish_multi_output_job(job_id, results)
            return
        project_id = await _ensure_flowkit_project(job_id, request) if output_count > 1 else request.project_id

        async def run_output(index: int) -> VideoResult:
            title = request.title if output_count == 1 else f"{request.title} - Variant {index + 1}"
            _update_progress(job_id, "output", f"Queued output {index + 1}/{output_count}")
            return await _client().generate_video(
                title=title,
                scenes=_scene_inputs(request.scenes),
                project_id=project_id,
                description=request.description,
                story=request.story,
                material=request.material,
                language=request.language,
                allow_music=request.allow_music,
                allow_voice=request.allow_voice,
                characters=_character_inputs(request.characters),
                generate_refs=request.generate_refs if index == 0 else False,
                orientation=request.orientation,
                video_gen_mode=request.video_gen_mode,
                upscale_4k=request.upscale_4k,
                on_progress=lambda stage, detail, output_index=index: _update_progress(
                    job_id,
                    stage,
                    f"Output {output_index + 1}/{output_count}: {detail}" if output_count > 1 else detail,
                ),
            )

        if output_count == 1:
            results = [await run_output(0)]
        else:
            _update_progress(job_id, "output", f"Generating {output_count} outputs in parallel in project {project_id}")
            results = await asyncio.gather(*(run_output(index) for index in range(output_count)))
        if output_count == 1:
            _finish_job(job_id, results[0])
        else:
            _finish_multi_output_job(job_id, results)
    except Exception as exc:
        _fail_job(job_id, str(exc))
        log.error("flowkit_job_failed", job_id=job_id, error=str(exc))


async def _run_simple_generate_job(job_id: str, request: FlowKitGenerateRequest, upload_path: str = "") -> None:
    try:
        await _run_generate_job(job_id, request)
    finally:
        if upload_path:
            Path(upload_path).unlink(missing_ok=True)


@router.get("/health")
async def health() -> dict[str, Any]:
    try:
        client = _client()
        status = await client.health()
        tier = await client.get_tier()
        return {"connected": bool(status.get("connected")), "tier": tier, "base_url": settings.flowkit_base_url}
    except Exception as exc:
        return {"connected": False, "base_url": settings.flowkit_base_url, "error": str(exc)}


@router.get("/materials")
async def materials() -> list[dict[str, Any]]:
    return await _client().list_materials()


@router.get("/models")
async def models() -> dict[str, Any]:
    return await _client().get_models()


@router.get("/projects")
async def projects(status: str = "") -> list[dict[str, Any]]:
    return await _client().list_projects(status or None)


@router.get("/projects/{project_id}")
async def project_detail(project_id: str) -> dict[str, Any]:
    try:
        return await _client().get_full_status(project_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/generate")
async def generate(request: FlowKitGenerateRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    if not request.scenes:
        raise HTTPException(status_code=400, detail="At least one scene is required.")
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "request": request.model_dump(),
        "progress": [],
        "result": None,
        "error": None,
    }
    await asyncio.to_thread(_upsert_job, job)
    background_tasks.add_task(_run_generate_job, job_id, request)
    return {"job_id": job_id, "status": "queued"}


@router.post("/generate/simple")
async def generate_simple(
    background_tasks: BackgroundTasks,
    prompt: str = Form(...),
    title: str = Form(""),
    project_id: str = Form(""),
    material: str = Form("realistic"),
    model: str = Form("default"),
    mode: str = Form("i2v"),
    output_count: int = Form(1),
    orientation: str = Form("VERTICAL"),
    image: UploadFile | None = File(None),
) -> dict[str, Any]:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        raise HTTPException(status_code=400, detail="Prompt is required.")
    safe_title = title.strip() or cleaned_prompt[:80]
    safe_orientation = "VERTICAL" if str(orientation or "").upper() in {"VERTICAL", "PORTRAIT", "9:16"} else "HORIZONTAL"
    upload_path = ""
    upload_name = ""
    if image and image.filename:
        suffix = Path(str(image.filename)).suffix or ".png"
        upload_dir = Path("data/flowkit_uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = str(upload_dir / f"{uuid.uuid4().hex}{suffix}")
        upload_name = str(image.filename)
        with Path(upload_path).open("wb") as handle:
            shutil.copyfileobj(image.file, handle)
    scene = FlowKitSceneRequest(
        prompt=cleaned_prompt,
        video_prompt=_simple_video_prompt(cleaned_prompt, safe_orientation),
        image_prompt=_simple_image_prompt(cleaned_prompt, material, safe_orientation),
        chain_type="ROOT",
        upload_image_path=upload_path or None,
    )
    request = FlowKitGenerateRequest(
        title=safe_title,
        scenes=[scene],
        project_id=project_id.strip() or None,
        material=_normalize_flowkit_material(material),
        orientation=safe_orientation,
        video_gen_mode=mode or "i2v",
        output_count=max(1, min(int(output_count or 1), 4)),
        model=model or "default",
        generate_refs=False,
    )
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "request": {
            **request.model_dump(),
            "simple": True,
            "upload_image_name": upload_name,
        },
        "progress": [],
        "result": None,
        "error": None,
    }
    await asyncio.to_thread(_upsert_job, job)
    background_tasks.add_task(_run_simple_generate_job, job_id, request, upload_path)
    return {"job_id": job_id, "status": "queued"}


@router.get("/media")
async def media_proxy(request: Request, url: str) -> Response:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid media URL.")
    headers: dict[str, str] = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    try:
        client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True)
        upstream = await client.stream("GET", url, headers=headers).__aenter__()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to fetch FlowKit media: {exc}") from exc

    async def stream_body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {
        "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
        "Cache-Control": "private, max-age=300",
    }
    for header in ["content-range", "content-type"]:
        if upstream.headers.get(header):
            response_headers[header.title()] = upstream.headers[header]
    return StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=response_headers,
    )


@router.post("/generate/quick")
async def generate_quick(request: FlowKitQuickGenerateRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    body = FlowKitGenerateRequest(
        title="Quick Video",
        scenes=[FlowKitSceneRequest(prompt=request.prompt, video_prompt=request.video_prompt)],
        material=request.material,
        orientation=request.orientation,
        upscale_4k=request.upscale_4k,
    )
    return await generate(body, background_tasks)


@router.get("/jobs")
async def jobs(limit: int = 50, status: str = "") -> dict[str, Any]:
    items = await asyncio.to_thread(_list_jobs, limit, status)
    return {"total": len(items), "jobs": items}


@router.get("/jobs/{job_id}")
async def job_detail(job_id: str) -> dict[str, Any]:
    job = await asyncio.to_thread(_get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="FlowKit job not found.")
    return job


@router.post("/upload-image")
async def upload_image(file: UploadFile = File(...), project_id: str = Form("")) -> dict[str, Any]:
    suffix = Path(str(file.filename or "")).suffix or ".png"
    temp_path = Path("data/flowkit_uploads") / f"{uuid.uuid4().hex}{suffix}"
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temp_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                handle.write(chunk)
        result = await _client().upload_image_file(str(temp_path), project_id, str(file.filename or "image.png"))
        return {"media_id": result.get("media_id"), "filename": file.filename, "raw": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        temp_path.unlink(missing_ok=True)
