from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.agents import facebook_spinner
from app.config import get_settings
from app.facebook_pages import _list_facebook_page_records, list_facebook_pages
from app.postgres import get_connection, serialize_json

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


settings = get_settings()
router = APIRouter(prefix=f"{settings.api_prefix}/facebook/content", tags=["facebook-content"])
UPLOAD_DIR = Path("data/facebook_uploads")
MAX_FACEBOOK_IMAGE_BYTES = 1_850_000


class FacebookContentImageInput(BaseModel):
    image_id: str = ""
    name: str = ""
    type: str = ""
    size: int = 0
    url: str = ""
    path: str = ""


class FacebookContentVariantRequest(BaseModel):
    brief: str = Field(..., min_length=1, max_length=8000)
    page_ids: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    images: list[FacebookContentImageInput] = Field(default_factory=list)
    tone: str = ""
    hashtag_count: int = Field(default=5, ge=0, le=12)
    core_caption_count: int | None = Field(default=None, ge=1, le=40)
    strategy: Literal["auto", "balanced"] = "auto"


class FacebookCoreCaption(BaseModel):
    angle: str = ""
    persona: str = ""
    headline: str = ""
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    cta: str = ""


class FacebookPostVariant(BaseModel):
    page_id: str
    page_name: str = ""
    group: str = ""
    angle: str = ""
    headline: str = ""
    cta: str = ""
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    core_index: int = 0


class FacebookContentVariantResponse(BaseModel):
    strategy: str
    page_count: int
    core_caption_count: int
    recommended_core_caption_count: int
    core_captions: list[FacebookCoreCaption] = Field(default_factory=list)
    posts: list[FacebookPostVariant] = Field(default_factory=list)
    quality: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class FacebookContentImageUploadItem(BaseModel):
    image_id: str
    name: str = ""
    type: str = ""
    original_size: int = 0
    stored_size: int = 0


class FacebookContentImageUploadResponse(BaseModel):
    total: int = 0
    images: list[FacebookContentImageUploadItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class FacebookContentJobCreateRequest(BaseModel):
    brief: str = Field(..., min_length=1, max_length=8000)
    variants: list[FacebookPostVariant] = Field(default_factory=list)
    images: list[FacebookContentImageInput] = Field(default_factory=list)
    publish_status: Literal["draft", "publish"] = "publish"


class FacebookContentJobResponse(BaseModel):
    job_id: str
    status: str = "draft"
    created_at: str = ""
    updated_at: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


def _now() -> str:
    return datetime.utcnow().isoformat()


def _job_path(job_id: str) -> Path:
    return Path("data/facebook_content_jobs") / f"{job_id}.json"


def _safe_image_path(image_id: str) -> Path | None:
    image_id = str(image_id or "").strip()
    if not image_id:
        return None
    for path in UPLOAD_DIR.glob(f"{image_id}.*"):
        if path.is_file():
            return path
    return None


def _upsert_job(job: dict[str, Any]) -> None:
    job["updated_at"] = _now()
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO facebook_content_jobs (job_id, status, updated_at, data)
                VALUES (%s, %s, NOW(), %s::jsonb)
                ON CONFLICT (job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (job["job_id"], job.get("status", "draft"), serialize_json(job)),
            )
        return
    path = _job_path(job["job_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_job(job_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM facebook_content_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
    path = _job_path(job_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT data::text FROM facebook_content_jobs ORDER BY updated_at DESC LIMIT %s",
                (max(1, min(limit, 200)),),
            )
            return [json.loads(row[0]) for row in cur.fetchall()]
    directory = Path("data/facebook_content_jobs")
    if not directory.exists():
        return []
    paths = sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def _compress_image(source: Path, target: Path) -> None:
    if Image is None:
        shutil.copyfile(source, target)
        return
    with Image.open(source) as image:
        image = image.convert("RGB")
        max_side = 2048
        image.thumbnail((max_side, max_side))
        for quality in [88, 82, 76, 70, 64, 58, 52]:
            image.save(target, format="JPEG", quality=quality, optimize=True)
            if target.stat().st_size <= MAX_FACEBOOK_IMAGE_BYTES:
                return
        image.thumbnail((1440, 1440))
        image.save(target, format="JPEG", quality=58, optimize=True)


def _page_token_map() -> dict[str, dict[str, Any]]:
    return {str(page.get("page_id") or ""): page for page in _list_facebook_page_records()}


def _publish_single_page(client: httpx.Client, page: dict[str, Any], variant: dict[str, Any], image_paths: list[Path]) -> dict[str, Any]:
    page_id = str(variant.get("page_id") or page.get("page_id") or "")
    token = str(page.get("page_access_token") or "")
    if not page_id or not token:
        raise RuntimeError("Missing page id or page token.")
    base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    caption = str(variant.get("caption") or "").strip()
    hashtags = " ".join(str(tag) for tag in variant.get("hashtags") or [] if str(tag).strip())
    message = f"{caption}\n\n{hashtags}".strip()
    attached_media = []
    with tempfile.TemporaryDirectory(prefix="cf-fb-post-") as temp_dir:
        temp = Path(temp_dir)
        for index, image_path in enumerate(image_paths[:10]):
            upload_path = temp / f"upload-{index}.jpg"
            _compress_image(image_path, upload_path)
            with upload_path.open("rb") as handle:
                response = client.post(
                    f"{base_url}/{page_id}/photos",
                    params={"access_token": token},
                    data={"published": "false"},
                    files={"source": (upload_path.name, handle, "image/jpeg")},
                )
            response.raise_for_status()
            media_id = str(response.json().get("id") or "")
            if media_id:
                attached_media.append({"media_fbid": media_id})
    if attached_media:
        feed_data = {"message": message}
        for index, item in enumerate(attached_media):
            feed_data[f"attached_media[{index}]"] = json.dumps(item)
        response = client.post(
            f"{base_url}/{page_id}/feed",
            params={"access_token": token},
            data=feed_data,
        )
    else:
        response = client.post(
            f"{base_url}/{page_id}/feed",
            params={"access_token": token},
            data={"message": message},
        )
    response.raise_for_status()
    payload = response.json()
    post_id = str(payload.get("id") or "")
    return {
        "page_id": page_id,
        "page_name": variant.get("page_name") or page.get("name") or "",
        "status": "published",
        "facebook_post_id": post_id,
        "permalink": f"https://www.facebook.com/{post_id}" if post_id else "",
        "published_at": _now(),
    }


def _run_publish_job(job_id: str) -> None:
    job = _get_job(job_id)
    if not job:
        return
    job["status"] = "posting"
    _upsert_job(job)
    pages = _page_token_map()
    image_paths = [path for image in job.get("images", []) if (path := _safe_image_path(str(image.get("image_id") or "")))]
    results = []
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for variant in job.get("variants", []):
            page_id = str(variant.get("page_id") or "")
            try:
                result = _publish_single_page(client, pages.get(page_id) or {}, variant, image_paths)
            except Exception as exc:
                result = {
                    "page_id": page_id,
                    "page_name": variant.get("page_name") or "",
                    "status": "failed",
                    "error": str(exc),
                    "failed_at": _now(),
                }
            results.append(result)
            job["results"] = results
            job["status"] = "posting"
            _upsert_job(job)
    failures = [item for item in results if item.get("status") == "failed"]
    job["status"] = "failed" if failures and len(failures) == len(results) else "completed"
    job["completed_at"] = _now()
    job["results"] = results
    _upsert_job(job)


def _select_pages(page_ids: list[str], groups: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    all_pages = list_facebook_pages()
    selected_ids = {str(item).strip() for item in page_ids if str(item).strip()}
    selected_groups = {str(item).strip() for item in groups if str(item).strip()}
    pages: list[dict[str, Any]] = []
    warnings: list[str] = []

    for page in all_pages:
        page_id = str(page.get("page_id") or "")
        group = str(page.get("group") or "").strip()
        if selected_ids and page_id in selected_ids:
            pages.append(page)
            continue
        if selected_groups and group in selected_groups:
            pages.append(page)

    if not selected_ids and not selected_groups:
        warnings.append("No page_ids or groups were provided; using all connected pages.")
        pages = all_pages

    found_ids = {str(page.get("page_id") or "") for page in pages}
    missing_ids = sorted(selected_ids - found_ids)
    if missing_ids:
        warnings.append(f"Some requested pages were not found: {', '.join(missing_ids[:5])}")

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        page_id = str(page.get("page_id") or "")
        if not page_id or page_id in seen:
            continue
        seen.add(page_id)
        deduped.append(page)
    return deduped, warnings


@router.post("/preview-variants", response_model=FacebookContentVariantResponse)
async def preview_facebook_content_variants(request: FacebookContentVariantRequest) -> FacebookContentVariantResponse:
    pages, warnings = await asyncio.to_thread(_select_pages, request.page_ids, request.groups)
    if not pages:
        raise HTTPException(status_code=400, detail="No Facebook pages matched the selected page_ids/groups.")
    result = await asyncio.to_thread(
        facebook_spinner.run,
        brief=request.brief,
        pages=pages,
        groups=request.groups,
        tone=request.tone,
        image_count=len(request.images),
        hashtag_count=request.hashtag_count,
        core_count=request.core_caption_count,
    )
    result["warnings"] = [*warnings, *(result.get("warnings") or [])]
    return FacebookContentVariantResponse(**result)


@router.post("/images", response_model=FacebookContentImageUploadResponse)
async def upload_facebook_content_images(files: list[UploadFile] = File(default=[])) -> FacebookContentImageUploadResponse:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    uploaded: list[FacebookContentImageUploadItem] = []
    warnings: list[str] = []
    for file in files[:10]:
        content_type = str(file.content_type or "")
        if not content_type.startswith("image/"):
            warnings.append(f"{file.filename}: skipped non-image file")
            continue
        image_id = uuid.uuid4().hex
        original_path = UPLOAD_DIR / f"{image_id}.source"
        target_path = UPLOAD_DIR / f"{image_id}.jpg"
        original_size = 0
        with original_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                original_size += len(chunk)
                handle.write(chunk)
        try:
            await asyncio.to_thread(_compress_image, original_path, target_path)
        finally:
            original_path.unlink(missing_ok=True)
        stored_size = target_path.stat().st_size
        if stored_size > MAX_FACEBOOK_IMAGE_BYTES:
            warnings.append(f"{file.filename}: compressed image is still above safe target ({stored_size} bytes)")
        uploaded.append(
            FacebookContentImageUploadItem(
                image_id=image_id,
                name=str(file.filename or ""),
                type="image/jpeg",
                original_size=original_size,
                stored_size=stored_size,
            )
        )
    return FacebookContentImageUploadResponse(total=len(uploaded), images=uploaded, warnings=warnings)


@router.post("/jobs", response_model=FacebookContentJobResponse)
async def create_facebook_content_job(request: FacebookContentJobCreateRequest, background_tasks: BackgroundTasks) -> FacebookContentJobResponse:
    if not request.variants:
        raise HTTPException(status_code=400, detail="At least one Facebook post variant is required.")
    job_id = uuid.uuid4().hex
    now = _now()
    job = {
        "job_id": job_id,
        "status": "queued" if request.publish_status == "publish" else "draft",
        "created_at": now,
        "updated_at": now,
        "brief": request.brief,
        "variants": [item.model_dump() for item in request.variants],
        "images": [item.model_dump() for item in request.images],
        "publish_status": request.publish_status,
        "results": [],
    }
    await asyncio.to_thread(_upsert_job, job)
    if request.publish_status == "publish":
        background_tasks.add_task(_run_publish_job, job_id)
    return FacebookContentJobResponse(job_id=job_id, status=job["status"], created_at=now, updated_at=now, data=job)


@router.get("/jobs")
async def list_facebook_content_jobs(limit: int = 50) -> dict[str, Any]:
    jobs = await asyncio.to_thread(_list_jobs, limit)
    return {"total": len(jobs), "jobs": jobs}


@router.get("/jobs/{job_id}", response_model=FacebookContentJobResponse)
async def get_facebook_content_job(job_id: str) -> FacebookContentJobResponse:
    job = await asyncio.to_thread(_get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Facebook content job not found.")
    return FacebookContentJobResponse(
        job_id=job_id,
        status=str(job.get("status") or "draft"),
        created_at=str(job.get("created_at") or ""),
        updated_at=str(job.get("updated_at") or ""),
        data=job,
    )
