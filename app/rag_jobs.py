from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from app.postgres import get_connection, serialize_json
from app.rag import ingest_text, ingest_url


LOCAL_JOB_DIR = Path("data/rag_jobs")


def _now() -> str:
    return datetime.utcnow().isoformat()


def _job_path(job_id: str) -> Path:
    return LOCAL_JOB_DIR / f"{job_id}.json"


def create_rag_job(kind: str, request: dict[str, Any]) -> dict[str, Any]:
    job = {
        "job_id": secrets.token_hex(6),
        "kind": kind,
        "status": "queued",
        "progress_percent": 0,
        "request": request,
        "result": None,
        "error": "",
        "created_at": _now(),
        "updated_at": _now(),
        "progress": [{"time": _now(), "stage": "queued", "detail": "RAG ingest queued", "percent": 0}],
    }
    upsert_rag_job(job)
    return job


def upsert_rag_job(job: dict[str, Any]) -> None:
    job["updated_at"] = _now()
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag_jobs (job_id, status, updated_at, data)
                VALUES (%s, %s, NOW(), %s::jsonb)
                ON CONFLICT (job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (job["job_id"], job.get("status", "queued"), serialize_json(job)),
            )
        return
    LOCAL_JOB_DIR.mkdir(parents=True, exist_ok=True)
    _job_path(job["job_id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def get_rag_job(job_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM rag_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
    path = _job_path(job_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_rag_jobs(limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 100))
    conn = get_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM rag_jobs ORDER BY updated_at DESC LIMIT %s", (limit,))
            return [json.loads(row[0]) for row in cur.fetchall()]
    if not LOCAL_JOB_DIR.exists():
        return []
    paths = sorted(LOCAL_JOB_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def public_rag_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(job, ensure_ascii=False, default=str))
    request = payload.get("request")
    if isinstance(request, dict):
        if "content" in request:
            content = str(request.get("content") or "")
            request["content_preview"] = content[:240]
            request.pop("content", None)
        if "upload_path" in request:
            request["upload_path"] = ""
    return payload


def _progress(job: dict[str, Any], stage: str, detail: str, percent: int) -> None:
    job.setdefault("progress", []).append({"time": _now(), "stage": stage, "detail": detail, "percent": percent})
    job["progress_percent"] = max(0, min(100, percent))
    upsert_rag_job(job)


def run_rag_job(job_id: str) -> None:
    job = get_rag_job(job_id)
    if not job:
        return
    request = job.get("request") or {}
    upload_path = request.get("upload_path") or ""
    try:
        job["status"] = "processing"
        _progress(job, "processing", "Preparing RAG ingest", 10)
        kind = str(job.get("kind") or "")
        if kind == "url":
            _progress(job, "fetch", "Fetching and extracting URL", 30)
            result = ingest_url(
                str(request.get("url") or ""),
                request.get("manual_categories") or [],
                request.get("manual_tags") or [],
                request.get("note"),
                bool(request.get("force_reingest", True)),
            )
        elif kind in {"text", "file"}:
            _progress(job, "extract", "Extracting text knowledge", 35)
            content = str(request.get("content") or "")
            if not content and upload_path:
                payload = Path(upload_path).read_bytes()
                try:
                    content = payload.decode("utf-8-sig")
                except UnicodeDecodeError:
                    content = payload.decode("latin-1")
            result = ingest_text(
                str(request.get("title") or ""),
                content,
                request.get("manual_categories") or [],
                request.get("manual_tags") or [],
                request.get("note"),
                request.get("source_id"),
                bool(request.get("force_reingest", True)),
            )
        else:
            raise ValueError(f"Unsupported RAG job kind: {kind}")
        job["status"] = "completed"
        job["result"] = result
        job["error"] = ""
        _progress(job, "completed", f"Ingested {result.get('documents_count', 0)} chunks", 100)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        _progress(job, "failed", str(exc), 100)
    finally:
        if upload_path:
            Path(upload_path).unlink(missing_ok=True)
