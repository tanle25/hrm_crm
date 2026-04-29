from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.config import get_settings
from app.facebook_pages import _list_facebook_page_records, list_facebook_pages
from app.postgres import get_connection, serialize_json


settings = get_settings()
router = APIRouter(prefix=f"{settings.api_prefix}/facebook/reels", tags=["facebook-reels"])
REEL_UPLOAD_DIR = Path("data/facebook_reel_uploads")
REEL_JOB_DIR = Path("data/facebook_reel_jobs")
MAX_REEL_BYTES = 1024 * 1024 * 1024


class FacebookReelVideoUploadResponse(BaseModel):
    video_id: str
    name: str = ""
    type: str = ""
    size: int = 0
    duration_sec: float = 0
    width: int = 0
    height: int = 0
    fps: float = 0
    warnings: list[str] = Field(default_factory=list)


class FacebookReelJobCreateRequest(BaseModel):
    video_id: str = Field(..., min_length=1)
    caption: str = Field(..., min_length=1, max_length=5000)
    title: str = Field(default="", max_length=255)
    page_ids: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    publish_status: Literal["publish", "scheduled"] = "publish"
    scheduled_at: str = ""
    schedule_mode: Literal["manual", "best_time", ""] = ""


def _now() -> str:
    return datetime.utcnow().isoformat()


def _job_path(job_id: str) -> Path:
    return REEL_JOB_DIR / f"{job_id}.json"


def _video_path(video_id: str) -> Path | None:
    for path in REEL_UPLOAD_DIR.glob(f"{video_id}.*"):
        if path.is_file() and not path.name.endswith(".json"):
            return path
    return None


def _metadata_path(video_id: str) -> Path:
    return REEL_UPLOAD_DIR / f"{video_id}.json"


def _upsert_job(job: dict[str, Any]) -> None:
    job["updated_at"] = _now()
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO facebook_reel_jobs (job_id, status, updated_at, data)
                VALUES (%s, %s, NOW(), %s::jsonb)
                ON CONFLICT (job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (job["job_id"], job.get("status", "queued"), serialize_json(job)),
            )
        return
    REEL_JOB_DIR.mkdir(parents=True, exist_ok=True)
    _job_path(job["job_id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_job(job_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM facebook_reel_jobs WHERE job_id = %s", (job_id,))
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
                "SELECT data::text FROM facebook_reel_jobs ORDER BY updated_at DESC LIMIT %s",
                (max(1, min(limit, 200)),),
            )
            return [json.loads(row[0]) for row in cur.fetchall()]
    if not REEL_JOB_DIR.exists():
        return []
    paths = sorted(REEL_JOB_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def _parse_scheduled_at(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _scheduled_timestamp(value: str) -> int | None:
    parsed = _parse_scheduled_at(value)
    if not parsed:
        return None
    if parsed < datetime.now(timezone.utc) + timedelta(minutes=10):
        raise RuntimeError("Scheduled publish time must be at least 10 minutes in the future.")
    return int(parsed.timestamp())


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
        elif selected_groups and group in selected_groups:
            pages.append(page)
    if not selected_ids and not selected_groups:
        warnings.append("No page_ids or groups were provided; using all connected pages.")
        pages = all_pages
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        page_id = str(page.get("page_id") or "")
        if not page_id or page_id in seen:
            continue
        seen.add(page_id)
        deduped.append(page)
    return deduped, warnings


def _page_token_map() -> dict[str, dict[str, Any]]:
    return {str(page.get("page_id") or ""): page for page in _list_facebook_page_records()}


def _probe_video(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Cannot validate video. ffprobe is required on the server.") from exc
    payload = json.loads(result.stdout or "{}")
    stream = (payload.get("streams") or [{}])[0]
    duration = float((payload.get("format") or {}).get("duration") or 0)
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    fps_raw = str(stream.get("r_frame_rate") or "0/1")
    num, _, den = fps_raw.partition("/")
    fps = (float(num or 0) / float(den or 1)) if float(den or 1) else 0
    return {"duration_sec": duration, "width": width, "height": height, "fps": fps}


def _validate_video(metadata: dict[str, Any], size: int) -> list[str]:
    warnings: list[str] = []
    width = int(metadata.get("width") or 0)
    height = int(metadata.get("height") or 0)
    duration = float(metadata.get("duration_sec") or 0)
    fps = float(metadata.get("fps") or 0)
    if size > MAX_REEL_BYTES:
        warnings.append("Video is very large; Facebook upload may fail or take a long time.")
    if duration < 4 or duration > 60:
        warnings.append("Facebook Reels usually requires video duration from 4 to 60 seconds.")
    if width < 540 or height < 960:
        warnings.append("Recommended minimum Reels resolution is 540x960.")
    if width and height and abs((width / height) - (9 / 16)) > 0.04:
        warnings.append("Recommended Reels aspect ratio is 9:16.")
    if fps and fps < 23:
        warnings.append("Recommended Reels frame rate is at least 23fps.")
    return warnings


def _cleanup_video(video_id: str) -> None:
    _metadata_path(video_id).unlink(missing_ok=True)
    path = _video_path(video_id)
    if path:
        path.unlink(missing_ok=True)


def _publish_reel_single_page(
    client: httpx.Client,
    page: dict[str, Any],
    video_path: Path,
    caption: str,
    title: str = "",
    scheduled_at: str = "",
) -> dict[str, Any]:
    page_id = str(page.get("page_id") or "")
    token = str(page.get("page_access_token") or "")
    if not page_id or not token:
        raise RuntimeError("Missing page id or page token.")
    base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    scheduled_ts = _scheduled_timestamp(scheduled_at) if scheduled_at else None

    start = client.post(
        f"{base_url}/{page_id}/video_reels",
        params={"access_token": token},
        data={"upload_phase": "start"},
    )
    start.raise_for_status()
    start_payload = start.json()
    reel_video_id = str(start_payload.get("video_id") or start_payload.get("id") or "")
    upload_url = str(start_payload.get("upload_url") or "")
    if not reel_video_id or not upload_url:
        raise RuntimeError(f"Facebook did not return Reel upload session: {start_payload}")

    with video_path.open("rb") as handle:
        upload = client.post(
            upload_url,
            headers={"Authorization": f"OAuth {token}", "file_offset": "0"},
            content=handle,
        )
    upload.raise_for_status()

    finish_data: dict[str, Any] = {
        "upload_phase": "finish",
        "video_id": reel_video_id,
        "description": caption,
    }
    if title:
        finish_data["title"] = title[:255]
    if scheduled_ts:
        finish_data["published"] = "false"
        finish_data["scheduled_publish_time"] = str(scheduled_ts)
    finish = client.post(
        f"{base_url}/{page_id}/video_reels",
        params={"access_token": token},
        data=finish_data,
    )
    finish.raise_for_status()
    finish_payload = finish.json()
    post_id = str(finish_payload.get("post_id") or finish_payload.get("id") or reel_video_id)
    return {
        "page_id": page_id,
        "page_name": page.get("name") or "",
        "status": "scheduled" if scheduled_ts else "published",
        "facebook_reel_id": reel_video_id,
        "facebook_post_id": post_id,
        "permalink": f"https://www.facebook.com/reel/{reel_video_id}" if reel_video_id else "",
        "published_at": "" if scheduled_ts else _now(),
        "scheduled_at": scheduled_at if scheduled_ts else "",
    }


def _run_reel_job(job_id: str) -> None:
    job = _get_job(job_id)
    if not job:
        return
    video_id = str(job.get("video_id") or "")
    video_path = _video_path(video_id)
    if not video_path:
        job["status"] = "failed"
        job["error"] = "Temporary video file is missing."
        _upsert_job(job)
        return
    job["status"] = "uploading"
    _upsert_job(job)
    pages = _page_token_map()
    results: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0)) as client:
            for target in job.get("targets", []):
                page_id = str(target.get("page_id") or "")
                try:
                    result = _publish_reel_single_page(
                        client,
                        pages.get(page_id) or {},
                        video_path,
                        str(job.get("caption") or ""),
                        str(job.get("title") or ""),
                        str(job.get("scheduled_at") or ""),
                    )
                except Exception as exc:
                    result = {
                        "page_id": page_id,
                        "page_name": target.get("page_name") or "",
                        "status": "failed",
                        "error": str(exc),
                        "failed_at": _now(),
                    }
                results.append(result)
                job["results"] = results
                job["status"] = "uploading"
                _upsert_job(job)
        failures = [item for item in results if item.get("status") == "failed"]
        if failures and len(failures) == len(results):
            job["status"] = "failed"
        elif job.get("publish_status") == "scheduled":
            job["status"] = "scheduled"
        else:
            job["status"] = "completed"
        job["completed_at"] = _now()
        job["results"] = results
        _upsert_job(job)
    finally:
        _cleanup_video(video_id)


@router.post("/videos", response_model=FacebookReelVideoUploadResponse)
async def upload_facebook_reel_video(file: UploadFile = File(...)) -> FacebookReelVideoUploadResponse:
    content_type = str(file.content_type or "")
    if not content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="Only video files are accepted for Facebook Reels.")
    REEL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    video_id = uuid.uuid4().hex
    suffix = Path(str(file.filename or "")).suffix.lower() or ".mp4"
    target = REEL_UPLOAD_DIR / f"{video_id}{suffix}"
    size = 0
    try:
        with target.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                handle.write(chunk)
        metadata = await asyncio.to_thread(_probe_video, target)
        warnings = _validate_video(metadata, size)
        saved = {
            "video_id": video_id,
            "name": str(file.filename or ""),
            "type": content_type,
            "size": size,
            **metadata,
            "warnings": warnings,
            "created_at": _now(),
        }
        _metadata_path(video_id).write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        return FacebookReelVideoUploadResponse(**saved)
    except Exception:
        target.unlink(missing_ok=True)
        raise


@router.post("/jobs")
async def create_facebook_reel_job(request: FacebookReelJobCreateRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    video_path = _video_path(request.video_id)
    metadata_path = _metadata_path(request.video_id)
    if not video_path or not metadata_path.exists():
        raise HTTPException(status_code=400, detail="Uploaded Reel video was not found or has expired.")
    if request.publish_status == "scheduled":
        try:
            if _scheduled_timestamp(request.scheduled_at) is None:
                raise RuntimeError("Scheduled publish time is required.")
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    pages, warnings = await asyncio.to_thread(_select_pages, request.page_ids, request.groups)
    if not pages:
        raise HTTPException(status_code=400, detail="No Facebook pages matched the selected page_ids/groups.")
    video_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    job_id = uuid.uuid4().hex
    now = _now()
    job = {
        "job_id": job_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "video_id": request.video_id,
        "video": video_metadata,
        "title": request.title,
        "caption": request.caption,
        "targets": [{"page_id": page.get("page_id"), "page_name": page.get("name"), "group": page.get("group", "")} for page in pages],
        "publish_status": request.publish_status,
        "scheduled_at": request.scheduled_at,
        "schedule_mode": request.schedule_mode,
        "results": [],
        "warnings": [*warnings, *(video_metadata.get("warnings") or [])],
    }
    await asyncio.to_thread(_upsert_job, job)
    background_tasks.add_task(_run_reel_job, job_id)
    return {"job_id": job_id, "status": job["status"], "created_at": now, "updated_at": now, "data": job}


@router.get("/jobs")
async def list_facebook_reel_jobs(limit: int = 50) -> dict[str, Any]:
    jobs = await asyncio.to_thread(_list_jobs, limit)
    return {"total": len(jobs), "jobs": jobs}


@router.get("/jobs/{job_id}")
async def get_facebook_reel_job(job_id: str) -> dict[str, Any]:
    job = await asyncio.to_thread(_get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Facebook Reel job not found.")
    return {"job_id": job_id, "status": job.get("status", "queued"), "data": job}
