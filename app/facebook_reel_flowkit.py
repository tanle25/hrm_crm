from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.config import get_settings
from app.facebook_reels import (
    _get_job,
    _list_jobs,
    _now,
    _page_token_map,
    _publish_reel_single_page,
    _scheduled_timestamp,
    _select_pages,
    _upsert_job,
    prepare_reel_video_for_upload,
)
from app.flowkit import _client, _crop_coordinates, _normalize_flowkit_material, _scene_media, _simple_image_prompt
from app.llm import call_json


settings = get_settings()
router = APIRouter(prefix=f"{settings.api_prefix}/facebook/reels/flowkit", tags=["facebook-reel-flowkit"])
FLOWKIT_REEL_UPLOAD_DIR = Path("data/facebook_reel_flowkit_uploads")
FLOWKIT_REEL_DOWNLOAD_DIR = Path("data/facebook_reel_flowkit_downloads")
PRODUCT_IMAGE_GUARD = (
    "Product fidelity rules: preserve the exact product identity, shape, proportions, color, labels, logo placement, "
    "material, packaging geometry, count of items, and visible defects from the reference/brief. Do not redesign the "
    "product, do not change text or labels, do not add/remove parts, do not invent variants, no warped geometry, no "
    "melted edges, no duplicated product, no deformed hands touching the product, no surreal morphing."
)
PRODUCT_VIDEO_GUARD = (
    "Critical product preservation: use the assigned image as the exact first frame/source of truth. The product must "
    "remain rigid and unchanged for the whole clip: same silhouette, dimensions, colors, labels, logo, package shape, "
    "texture, and item count. Do not morph, bend, melt, stretch, shrink, duplicate, replace, rotate into a different "
    "object, or change packaging text. Only animate camera movement, lighting, shallow depth of field, background "
    "atmosphere, and subtle environmental motion. Keep product edges stable and sharp."
)


class FacebookReelFlowKitJobCreateResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    data: dict[str, Any] = Field(default_factory=dict)


class FacebookReelFlowKitPublishRequest(BaseModel):
    indexes: list[int] = Field(default_factory=list)


class FacebookReelFlowKitRegenerateRequest(BaseModel):
    index: int = Field(..., ge=0)


def _safe_text(value: Any, max_len: int = 5000) -> str:
    return " ".join(str(value or "").strip().split())[:max_len]


def _visual_prompt_seed(brief: str, page: dict[str, Any], title: str) -> str:
    page_name = page.get("name") or "Facebook page"
    category = page.get("category") or page.get("group") or "local business"
    return (
        f"Create a realistic vertical 9:16 Facebook Reel for {page_name}, a {category}. "
        f"Topic: {brief or title}. Commercial social video, clear subject, natural motion, "
        "mobile-first composition, no text overlay, no watermark. "
        f"{PRODUCT_IMAGE_GUARD}"
    )


def _video_prompt_for_page(brief: str, page: dict[str, Any], variant: int, title: str) -> str:
    page_name = page.get("name") or "Facebook page"
    variants = [
        "0-2s: hook with a close-up reveal. 2-5s: smooth push-in with natural subject motion. 5-8s: hold a clean product/lifestyle hero shot.",
        "0-2s: start with a dynamic side movement. 2-5s: show practical detail and atmosphere. 5-8s: end with a confident commercial final frame.",
        "0-2s: slow cinematic reveal. 2-5s: add subtle hand-held realism and environmental motion. 5-8s: settle into a polished mobile ad shot.",
    ]
    return (
        "True vertical 9:16 mobile Reel, fill the entire frame edge to edge, no black bars. "
        f"{PRODUCT_VIDEO_GUARD} "
        "No voiceover, no spoken dialogue, no narration; background music only. "
        f"Brand/page context: {page_name}. Topic: {brief or title}. "
        f"{variants[variant % len(variants)]}"
    )


def _guard_image_prompt(prompt: str) -> str:
    prompt = _safe_text(prompt, 3000)
    if PRODUCT_IMAGE_GUARD in prompt:
        return prompt
    return f"{prompt}\n\n{PRODUCT_IMAGE_GUARD}\nPhotorealistic product-ad first frame, stable geometry, clean commercial lighting."


def _guard_video_prompt(prompt: str) -> str:
    prompt = _safe_text(prompt, 3000)
    if PRODUCT_VIDEO_GUARD in prompt:
        return prompt
    return (
        f"{PRODUCT_VIDEO_GUARD}\n"
        "True vertical 9:16, edge-to-edge frame, no black bars. No voiceover, no spoken dialogue, background music only.\n"
        f"{prompt}"
    )


def _caption_for_page(brief: str, page: dict[str, Any], title: str, cta: str) -> str:
    page_name = page.get("name") or "Page"
    hook = title or brief or "Mẫu mới hôm nay"
    cta_text = cta or "Inbox page để được tư vấn nhanh. Giao hàng toàn quốc, nhận hàng kiểm tra rồi thanh toán."
    return (
        f"🔥 {hook}\n\n"
        f"{brief}\n\n" if brief else f"Video mới từ {page_name}.\n\n"
    ) + f"✅ {cta_text}\n\n#reels #facebookreels #banhangonline"


def _build_reel_plan(
    *,
    pages: list[dict[str, Any]],
    brief: str,
    title: str,
    cta: str,
    image_count: int,
    videos_per_page: int,
) -> list[dict[str, Any]]:
    expanded = [
        {"page": page, "variant": variant}
        for page in pages
        for variant in range(max(1, min(videos_per_page, 3)))
    ]
    fallback = {
        "items": [
            {
                "page_id": item["page"].get("page_id"),
                "page_name": item["page"].get("name"),
                "variant": item["variant"] + 1,
                "title": title or _safe_text(brief, 90) or "Facebook Reel",
                "caption": _caption_for_page(brief, item["page"], title, cta),
                "image_index": (index % image_count) if image_count > 0 else None,
                "image_prompt": _guard_image_prompt(_simple_image_prompt(_visual_prompt_seed(brief, item["page"], title), "realistic", "VERTICAL")),
                "video_prompt": _guard_video_prompt(_video_prompt_for_page(brief, item["page"], item["variant"], title)),
            }
            for index, item in enumerate(expanded)
        ]
    }
    try:
        page_context = [
            {
                "page_id": page.get("page_id"),
                "name": page.get("name"),
                "category": page.get("category"),
                "group": page.get("group"),
            }
            for page in pages
        ]
        data = call_json(
            "facebook_spinner",
            "Bạn là strategist viết Facebook Reels bán hàng. Trả JSON hợp lệ duy nhất.",
            (
                "Tạo kế hoạch Reels cho từng fanpage. Mỗi item phải có: page_id, page_name, variant, title, "
                "caption tiếng Việt có hook + lợi ích + CTA, image_index nếu có ảnh upload, image_prompt tiếng Anh, "
                "video_prompt tiếng Anh mô tả chuyển động 8 giây 9:16, không voiceover/không lời thoại/chỉ nhạc nền. "
                "Bắt buộc giữ nguyên sản phẩm: không đổi hình dáng, màu, nhãn, logo, số lượng, bao bì; không làm méo, chảy, biến dạng, nhân bản hoặc morph sản phẩm. "
                "Nếu có ảnh upload, coi ảnh đó là source of truth; chỉ mô tả chuyển động camera/ánh sáng/nền, không thay đổi sản phẩm. Không thêm giải thích ngoài JSON.\n"
                f"Brief: {brief}\nTitle: {title}\nCTA: {cta}\nImage count: {image_count}\nVideos per page: {videos_per_page}\nPages: {json.dumps(page_context, ensure_ascii=False)}"
            ),
            fallback=fallback,
            max_tokens=2600,
        )
        items = data.get("items")
        if isinstance(items, list) and items:
            by_page = {str(page.get("page_id")): page for page in pages}
            normalized: list[dict[str, Any]] = []
            for index, item in enumerate(items[: len(expanded)]):
                page_id = str(item.get("page_id") or expanded[index]["page"].get("page_id") or "")
                page = by_page.get(page_id) or expanded[index]["page"]
                image_index = item.get("image_index")
                if image_count <= 0:
                    image_index = None
                else:
                    try:
                        image_index = int(image_index) % image_count
                    except Exception:
                        image_index = index % image_count
                normalized.append(
                    {
                        "page_id": page.get("page_id"),
                        "page_name": page.get("name"),
                        "variant": int(item.get("variant") or expanded[index]["variant"] + 1),
                        "title": _safe_text(item.get("title") or title or brief, 255),
                        "caption": str(item.get("caption") or _caption_for_page(brief, page, title, cta))[:5000],
                        "image_index": image_index,
                        "image_prompt": _guard_image_prompt(str(item.get("image_prompt") or _visual_prompt_seed(brief, page, title))),
                        "video_prompt": _guard_video_prompt(str(item.get("video_prompt") or _video_prompt_for_page(brief, page, expanded[index]["variant"], title))),
                    }
                )
            return normalized
    except Exception:
        pass
    return fallback["items"]


def _job_progress(job: dict[str, Any], stage: str, detail: str, percent: int | None = None) -> None:
    job.setdefault("progress", []).append({"time": _now(), "stage": stage, "detail": detail, **({"percent": percent} if percent is not None else {})})
    if percent is not None:
        job["progress_percent"] = max(0, min(100, percent))
    _upsert_job(job)


async def _download_video(url: str, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=20.0), follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    handle.write(chunk)
    return size


def _flowkit_reel_job(job_id: str) -> dict[str, Any] | None:
    return _get_job(job_id) or next((item for item in _list_jobs(200) if item.get("job_id") == job_id), None)


def _replace_result(results: list[dict[str, Any]], index: int, result: dict[str, Any]) -> list[dict[str, Any]]:
    kept = [item for item in results if int(item.get("index", -1)) != index]
    kept.append(result)
    return sorted(kept, key=lambda item: int(item.get("index", 0)))


async def _run_flowkit_reel_job(job_id: str) -> None:
    job = _flowkit_reel_job(job_id)
    if not job:
        return
    request = job.get("request") or {}
    upload_paths = [Path(path) for path in job.get("upload_paths") or []]
    try:
        job["status"] = "processing"
        _job_progress(job, "plan", "Creating per-page Reel plan...", 5)

        pages, warnings = _select_pages(request.get("page_ids") or [], request.get("groups") or [])
        if not pages:
            raise RuntimeError("No Facebook pages matched the selected page_ids/groups.")
        videos_per_page = max(1, min(int(request.get("videos_per_page") or 1), 3))
        plan = _build_reel_plan(
            pages=pages,
            brief=str(request.get("brief") or ""),
            title=str(request.get("title") or ""),
            cta=str(request.get("cta") or ""),
            image_count=len(upload_paths),
            videos_per_page=videos_per_page,
        )
        job["plan"] = plan
        job["warnings"] = [*(job.get("warnings") or []), *warnings]
        _job_progress(job, "flowkit", "Creating shared FlowKit project...", 10)

        client = _client()
        material = _normalize_flowkit_material(str(request.get("material") or "realistic"))
        project = await client.create_project(
            name=str(request.get("title") or "Facebook Reels FlowKit")[:255],
            description=str(request.get("brief") or ""),
            material=material,
            language="vi",
            allow_music=True,
            allow_voice=False,
        )
        project_id = str(project.get("id") or project.get("project_id") or "")
        if not project_id:
            raise RuntimeError(f"FlowKit create_project returned no project id: {project}")
        video = await client.create_video_container(project_id, str(request.get("title") or "Facebook Reels"), orientation="VERTICAL")
        flowkit_video_id = str(video.get("id") or "")
        if not flowkit_video_id:
            raise RuntimeError(f"FlowKit create video returned no id: {video}")
        job["flowkit"] = {"project_id": project_id, "video_id": flowkit_video_id}

        uploaded_images: list[dict[str, str]] = []
        for index, path in enumerate(upload_paths):
            _job_progress(job, "images", f"Uploading source image {index + 1}/{len(upload_paths)} to FlowKit...", 12 + min(18, index * 3))
            upload = await client.upload_image_file(str(path), project_id, path.name)
            media_id = str(upload.get("media_id") or upload.get("id") or "")
            if not media_id:
                raise RuntimeError(f"FlowKit image upload returned no media id: {upload}")
            uploaded_images.append({"media_id": media_id, "url": str(upload.get("url") or upload.get("image_url") or ""), "path": str(path)})

        scenes: list[dict[str, Any]] = []
        orient_prefix = "vertical"
        _job_progress(job, "scenes", f"Creating {len(plan)} scenes for selected pages...", 30)
        for index, item in enumerate(plan):
            image_prompt = _guard_image_prompt(item.get("image_prompt") or "")
            video_prompt = _guard_video_prompt(item.get("video_prompt") or "")
            item["image_prompt"] = image_prompt
            item["video_prompt"] = video_prompt
            scene = await client.create_scene(
                video_id=flowkit_video_id,
                prompt=image_prompt or video_prompt or request.get("brief") or "",
                image_prompt=image_prompt,
                video_prompt=video_prompt,
                display_order=index,
                chain_type="ROOT",
                orientation="VERTICAL",
            )
            scene_id = str(scene.get("id") or "")
            image_index = item.get("image_index")
            if uploaded_images and image_index is not None:
                source = uploaded_images[int(image_index) % len(uploaded_images)]
                update_payload: dict[str, Any] = {
                    f"{orient_prefix}_image_media_id": source["media_id"],
                    f"{orient_prefix}_image_status": "COMPLETED",
                }
                crop = _crop_coordinates(source.get("path") or "", "VERTICAL")
                if crop:
                    update_payload[f"{orient_prefix}_image_crop_coordinates"] = json.dumps(crop, separators=(",", ":"))
                await client.update_scene(scene_id, **update_payload)
            scenes.append({"scene_id": scene_id, **item})
        job["scenes"] = scenes

        if not uploaded_images:
            _job_progress(job, "images", "Generating first frames for scenes in batch...", 38)
            await client.submit_batch([
                {
                    "type": "GENERATE_IMAGE",
                    "orientation": "VERTICAL",
                    "scene_id": scene["scene_id"],
                    "project_id": project_id,
                    "video_id": flowkit_video_id,
                }
                for scene in scenes
            ])
            while True:
                status = await client.get_batch_status(video_id=flowkit_video_id, req_type="GENERATE_IMAGE", orientation="VERTICAL")
                total = max(1, int(status.get("total") or len(scenes)))
                done = int(status.get("completed") or 0) + int(status.get("failed") or 0)
                _job_progress(job, "images", f"Image progress {done}/{total}", 38 + round((done / total) * 18))
                if status.get("done"):
                    if int(status.get("failed") or 0):
                        raise RuntimeError(f"FlowKit image generation failed: {status}")
                    break
                await asyncio.sleep(client.poll_interval)

        _job_progress(job, "videos", "Queueing FlowKit videos in batch...", 58)
        await client.submit_batch([
            {
                "type": "GENERATE_VIDEO",
                "orientation": "VERTICAL",
                "scene_id": scene["scene_id"],
                "project_id": project_id,
                "video_id": flowkit_video_id,
            }
            for scene in scenes
        ])
        while True:
            status = await client.get_batch_status(video_id=flowkit_video_id, req_type="GENERATE_VIDEO", orientation="VERTICAL")
            total = max(1, int(status.get("total") or len(scenes)))
            completed = int(status.get("completed") or 0)
            failed = int(status.get("failed") or 0)
            processing = int(status.get("processing") or 0)
            pct = 58 + min(27, round(((completed + failed + processing * 0.5) / total) * 27))
            _job_progress(job, "videos", f"Video progress {completed}/{total} completed, {processing} processing", pct)
            if status.get("done"):
                if failed:
                    raise RuntimeError(f"FlowKit video generation failed: {status}")
                break
            await asyncio.sleep(client.poll_interval)

        scene_states = {str(scene.get("id") or ""): scene for scene in await client.list_scenes(flowkit_video_id)}
        review_items: list[dict[str, Any]] = []
        for index, scene in enumerate(scenes):
            state = scene_states.get(scene["scene_id"]) or await client.get_scene(scene["scene_id"])
            _, video_url = _scene_media(state, "VERTICAL", "video")
            if not video_url:
                raise RuntimeError(f"Scene {scene['scene_id']} completed but returned no video URL.")
            review_items.append(
                {
                    "index": index,
                    "status": "ready",
                    "page_id": scene.get("page_id"),
                    "page_name": scene.get("page_name") or "",
                    "title": scene.get("title") or request.get("title") or "",
                    "caption": scene.get("caption") or "",
                    "flowkit_scene_id": scene["scene_id"],
                    "flowkit_video_url": video_url,
                    "image_prompt": scene.get("image_prompt") or "",
                    "video_prompt": scene.get("video_prompt") or "",
                    "created_at": _now(),
                }
            )
        job["review_items"] = review_items
        job["results"] = []
        job["status"] = "ready_for_review"
        job["completed_at"] = _now()
        job["progress_percent"] = 100
        _job_progress(job, "review", "FlowKit videos are ready for review before publishing.", 100)
        _upsert_job(job)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["failed_at"] = _now()
        _upsert_job(job)
    finally:
        for path in upload_paths:
            Path(path).unlink(missing_ok=True)


def _run_flowkit_reel_job_sync(job_id: str) -> None:
    asyncio.run(_run_flowkit_reel_job(job_id))


async def _publish_flowkit_reel_job(job_id: str, indexes: list[int]) -> None:
    job = _flowkit_reel_job(job_id)
    if not job:
        return
    request = job.get("request") or {}
    review_items = job.get("review_items") or []
    if not isinstance(review_items, list) or not review_items:
        job["status"] = "failed"
        job["error"] = "No generated FlowKit videos are ready for review."
        _upsert_job(job)
        return
    selected = set(indexes or [int(item.get("index", idx)) for idx, item in enumerate(review_items) if item.get("status") == "ready"])
    temp_videos: list[Path] = []
    results = list(job.get("results") or [])
    pages_by_id = _page_token_map()
    job["status"] = "publishing"
    _job_progress(job, "publish", f"Publishing {len(selected)} approved Reels...", 90)
    try:
        with httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0)) as fb_client:
            for item in review_items:
                index = int(item.get("index", -1))
                if index not in selected:
                    continue
                video_url = str(item.get("flowkit_video_url") or "")
                if not video_url:
                    result = {**item, "status": "failed", "error": "Generated video URL is missing.", "failed_at": _now()}
                else:
                    item["status"] = "publishing"
                    _upsert_job(job)
                    local_video = FLOWKIT_REEL_DOWNLOAD_DIR / f"{job_id}-{index}.mp4"
                    prepared_video = FLOWKIT_REEL_DOWNLOAD_DIR / f"{job_id}-{index}-facebook.mp4"
                    temp_videos.extend([local_video, prepared_video])
                    try:
                        await _download_video(video_url, local_video)
                        upload_video = prepare_reel_video_for_upload(local_video, prepared_video)
                        result = _publish_reel_single_page(
                            fb_client,
                            pages_by_id.get(str(item.get("page_id"))) or {},
                            upload_video,
                            str(item.get("caption") or request.get("caption") or ""),
                            str(item.get("title") or request.get("title") or ""),
                            str(request.get("scheduled_at") or ""),
                        )
                    except Exception as exc:
                        result = {
                            "page_id": item.get("page_id"),
                            "page_name": item.get("page_name") or "",
                            "status": "failed",
                            "error": str(exc),
                            "failed_at": _now(),
                        }
                result.update(
                    {
                        "index": index,
                        "flowkit_scene_id": item.get("flowkit_scene_id") or "",
                        "flowkit_video_url": video_url,
                        "caption": item.get("caption") or "",
                        "title": item.get("title") or "",
                    }
                )
                item.update(result)
                results = _replace_result(results, index, result)
                job["results"] = results
                _upsert_job(job)
        statuses = [str(item.get("status") or "") for item in review_items]
        if any(status == "ready" for status in statuses):
            job["status"] = "ready_for_review"
        elif statuses and all(status == "failed" for status in statuses):
            job["status"] = "failed"
        elif request.get("publish_status") == "scheduled":
            job["status"] = "scheduled"
        else:
            job["status"] = "completed"
        job["completed_at"] = _now()
        _upsert_job(job)
    finally:
        for path in temp_videos:
            path.unlink(missing_ok=True)


def _publish_flowkit_reel_job_sync(job_id: str, indexes: list[int]) -> None:
    asyncio.run(_publish_flowkit_reel_job(job_id, indexes))


async def _regenerate_flowkit_reel_item(job_id: str, index: int) -> None:
    job = _flowkit_reel_job(job_id)
    if not job:
        return
    review_items = job.get("review_items") or []
    item = next((entry for entry in review_items if int(entry.get("index", -1)) == index), None)
    flowkit = job.get("flowkit") or {}
    if not item or not flowkit.get("project_id") or not flowkit.get("video_id") or not item.get("flowkit_scene_id"):
        return
    try:
        item["status"] = "regenerating"
        item["error"] = ""
        job["status"] = "processing"
        _job_progress(job, "videos", f"Regenerating video #{index + 1}...", 70)
        client = _client()
        request = await client.submit_request(
            "REGENERATE_VIDEO",
            project_id=str(flowkit["project_id"]),
            scene_id=str(item["flowkit_scene_id"]),
            video_id=str(flowkit["video_id"]),
            orientation="VERTICAL",
        )
        request_id = str(request.get("id") or request.get("request_id") or "")
        if request_id:
            await client.poll_request(request_id, client.video_timeout, f"FlowKit regenerate scene {item['flowkit_scene_id']}")
        state = await client.get_scene(str(item["flowkit_scene_id"]))
        _, video_url = _scene_media(state, "VERTICAL", "video")
        if not video_url:
            raise RuntimeError("FlowKit regenerate completed but returned no video URL.")
        item["flowkit_video_url"] = video_url
        item["status"] = "ready"
        item["regenerated_at"] = _now()
        job["status"] = "ready_for_review"
        _upsert_job(job)
    except Exception as exc:
        item["status"] = "failed"
        item["error"] = str(exc)
        job["status"] = "ready_for_review"
        _upsert_job(job)


def _regenerate_flowkit_reel_item_sync(job_id: str, index: int) -> None:
    asyncio.run(_regenerate_flowkit_reel_item(job_id, index))


@router.post("/jobs", response_model=FacebookReelFlowKitJobCreateResponse)
async def create_facebook_reel_flowkit_job(
    background_tasks: BackgroundTasks,
    brief: str = Form(...),
    title: str = Form(""),
    cta: str = Form(""),
    page_ids: str = Form("[]"),
    groups: str = Form("[]"),
    publish_status: Literal["publish", "scheduled"] = Form("publish"),
    scheduled_at: str = Form(""),
    videos_per_page: int = Form(1),
    material: str = Form("realistic"),
    images: list[UploadFile] = File(default=[]),
) -> FacebookReelFlowKitJobCreateResponse:
    clean_brief = _safe_text(brief, 2000)
    if not clean_brief:
        raise HTTPException(status_code=400, detail="Brief is required.")
    if publish_status == "scheduled":
        try:
            if _scheduled_timestamp(scheduled_at) is None:
                raise RuntimeError("Scheduled publish time is required.")
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        selected_page_ids = json.loads(page_ids or "[]")
        selected_groups = json.loads(groups or "[]")
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="page_ids/groups must be JSON arrays.") from error
    pages, warnings = await asyncio.to_thread(_select_pages, selected_page_ids, selected_groups)
    if not pages:
        raise HTTPException(status_code=400, detail="No Facebook pages matched the selected page_ids/groups.")

    FLOWKIT_REEL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_paths: list[str] = []
    for file in images or []:
        if not file.filename:
            continue
        if file.content_type and not str(file.content_type).startswith("image/"):
            raise HTTPException(status_code=400, detail="FlowKit Reel source files must be images.")
        suffix = Path(str(file.filename)).suffix or ".png"
        target = FLOWKIT_REEL_UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
        with target.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
        upload_paths.append(str(target))

    job_id = uuid.uuid4().hex[:12]
    now = _now()
    job = {
        "job_id": job_id,
        "status": "queued",
        "source": "flowkit",
        "created_at": now,
        "updated_at": now,
        "request": {
            "brief": clean_brief,
            "title": _safe_text(title, 255),
            "cta": _safe_text(cta, 1000),
            "page_ids": selected_page_ids,
            "groups": selected_groups,
            "publish_status": publish_status,
            "scheduled_at": scheduled_at,
            "videos_per_page": max(1, min(int(videos_per_page or 1), 3)),
            "material": _normalize_flowkit_material(material),
            "image_count": len(upload_paths),
        },
        "upload_paths": upload_paths,
        "targets": [{"page_id": page.get("page_id"), "page_name": page.get("name"), "group": page.get("group", "")} for page in pages],
        "results": [],
        "progress": [],
        "progress_percent": 0,
        "warnings": warnings,
    }
    await asyncio.to_thread(_upsert_job, job)
    background_tasks.add_task(_run_flowkit_reel_job_sync, job_id)
    return FacebookReelFlowKitJobCreateResponse(job_id=job_id, status="queued", created_at=now, updated_at=now, data=job)


@router.post("/jobs/{job_id}/publish")
async def publish_facebook_reel_flowkit_job(
    job_id: str,
    request: FacebookReelFlowKitPublishRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    job = await asyncio.to_thread(_flowkit_reel_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="FlowKit Reel job not found.")
    review_items = job.get("review_items") or []
    if not review_items:
        raise HTTPException(status_code=400, detail="No generated videos are ready for review.")
    if request.indexes:
        valid_indexes = {int(item.get("index", -1)) for item in review_items}
        missing = [index for index in request.indexes if index not in valid_indexes]
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown review item indexes: {missing}")
    job["status"] = "publishing"
    await asyncio.to_thread(_upsert_job, job)
    background_tasks.add_task(_publish_flowkit_reel_job_sync, job_id, request.indexes)
    return {"job_id": job_id, "status": "publishing", "indexes": request.indexes}


@router.post("/jobs/{job_id}/regenerate")
async def regenerate_facebook_reel_flowkit_item(
    job_id: str,
    request: FacebookReelFlowKitRegenerateRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    job = await asyncio.to_thread(_flowkit_reel_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="FlowKit Reel job not found.")
    review_items = job.get("review_items") or []
    if not any(int(item.get("index", -1)) == request.index for item in review_items):
        raise HTTPException(status_code=400, detail=f"Unknown review item index: {request.index}")
    job["status"] = "regenerating"
    await asyncio.to_thread(_upsert_job, job)
    background_tasks.add_task(_regenerate_flowkit_reel_item_sync, job_id, request.index)
    return {"job_id": job_id, "status": "regenerating", "index": request.index}
