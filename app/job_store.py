from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Condition, Lock
from typing import Any, Dict, Optional

from app.config import get_settings
from app.postgres import get_connection as _pg_connection, postgres_available, serialize_json

try:
    from redis import Redis
except ImportError:  # pragma: no cover
    Redis = None


STORE_PATH = Path("data/job_store.json")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _serialize(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _deserialize(raw: str | bytes | None) -> Optional[dict]:
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    payload = json.loads(raw)
    failed_at = payload.get("failed_at")
    if isinstance(failed_at, str):
        try:
            payload["failed_at"] = datetime.fromisoformat(failed_at)
        except ValueError:
            pass
    updated_at = payload.get("updated_at")
    if isinstance(updated_at, str):
        try:
            payload["updated_at"] = datetime.fromisoformat(updated_at)
        except ValueError:
            pass
    return payload


def _ensure_store_file() -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STORE_PATH.exists():
        STORE_PATH.write_text(json.dumps({"jobs": {}, "processed_url_hashes": {}, "dlq": []}, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_local_store() -> tuple[dict[str, dict], dict[str, dict], deque]:
    _ensure_store_file()
    try:
        payload = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {"jobs": {}, "processed_url_hashes": {}, "dlq": []}
    jobs = {key: _deserialize(_serialize(value)) or value for key, value in (payload.get("jobs") or {}).items()}
    processed = {key: _deserialize(_serialize(value)) or value for key, value in (payload.get("processed_url_hashes") or {}).items()}
    dlq_entries = deque(_deserialize(_serialize(item)) or item for item in (payload.get("dlq") or []))
    return jobs, processed, dlq_entries


def _persist_local_store() -> None:
    _ensure_store_file()
    payload = {
        "jobs": STORE.jobs,
        "processed_url_hashes": STORE.processed_url_hashes,
        "dlq": list(STORE.dlq),
    }
    STORE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")


def _redis_client() -> Redis | None:
    settings = get_settings()
    if Redis is None or settings.queue_mode != "rq":
        return None
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        return None


def publish_realtime_event(channel: str, payload: dict) -> None:
    redis_conn = _redis_client()
    if redis_conn is None:
        return
    event = {
        "channel": channel,
        "payload": payload,
        "published_at": datetime.utcnow().isoformat(),
    }
    try:
        redis_conn.publish(f"content_forge:realtime:{channel}", _serialize(event))
    except Exception:
        return


def publish_job_realtime_event(job_id: str, event_type: str) -> None:
    payload = {"type": event_type, "job_id": job_id}
    publish_realtime_event("jobs", payload)
    publish_realtime_event(f"job:{job_id}", payload)


def _postgres_conn():
    return _pg_connection() if postgres_available() else None


@dataclass
class _Store:
    jobs: Dict[str, dict]
    processed_url_hashes: Dict[str, dict]
    dlq: deque
    lock: Lock
    version_lock: Lock
    version_condition: Condition
    jobs_version: int


_version_lock = Lock()
STORE = _Store(
    jobs={},
    processed_url_hashes={},
    dlq=deque(),
    lock=Lock(),
    version_lock=_version_lock,
    version_condition=Condition(_version_lock),
    jobs_version=0,
)

if _redis_client() is None:
    jobs, processed_url_hashes, dlq = _load_local_store()
    STORE.jobs = jobs
    STORE.processed_url_hashes = processed_url_hashes
    STORE.dlq = dlq


def _bump_jobs_version() -> None:
    with STORE.version_condition:
        STORE.jobs_version += 1
        STORE.version_condition.notify_all()


def get_jobs_version() -> int:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT value FROM job_meta WHERE key = 'jobs_version'")
            row = cur.fetchone()
            return int(row[0]) if row else 0
    redis_conn = _redis_client()
    if redis_conn is not None:
        raw = redis_conn.get("content_forge:jobs:version")
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0
    with STORE.version_condition:
        return STORE.jobs_version


def wait_for_jobs_version(current_version: int, timeout_sec: float = 20.0) -> int:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        import time

        deadline = time.time() + max(timeout_sec, 0.1)
        latest = get_jobs_version()
        while latest <= current_version and time.time() < deadline:
            time.sleep(0.25)
            latest = get_jobs_version()
        return latest
    redis_conn = _redis_client()
    if redis_conn is not None:
        import time

        deadline = time.time() + max(timeout_sec, 0.1)
        latest = get_jobs_version()
        while latest <= current_version and time.time() < deadline:
            time.sleep(0.25)
            latest = get_jobs_version()
        return latest
    with STORE.version_condition:
        if STORE.jobs_version > current_version:
            return STORE.jobs_version
        STORE.version_condition.wait(timeout=max(timeout_sec, 0.1))
        return STORE.jobs_version


def save_job(job_id: str, payload: dict) -> None:
    payload["updated_at"] = datetime.utcnow()
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (job_id, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                ON CONFLICT (job_id) DO UPDATE SET
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (job_id, serialize_json(payload)),
            )
            cur.execute("UPDATE job_meta SET value = value + 1 WHERE key = 'jobs_version'")
        publish_job_realtime_event(job_id, "job.updated")
        return
    redis_conn = _redis_client()
    if redis_conn is not None:
        redis_conn.set(f"content_forge:job:{job_id}", _serialize(payload))
        redis_conn.sadd("content_forge:jobs", job_id)
        redis_conn.incr("content_forge:jobs:version")
        publish_job_realtime_event(job_id, "job.updated")
        return
    with STORE.lock:
        STORE.jobs[job_id] = payload
        _persist_local_store()
    _bump_jobs_version()


def get_job(job_id: str) -> Optional[dict]:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            return _deserialize(row[0]) if row else None
    redis_conn = _redis_client()
    if redis_conn is not None:
        return _deserialize(redis_conn.get(f"content_forge:job:{job_id}"))
    with STORE.lock:
        return STORE.jobs.get(job_id)


def list_jobs(status: str | None = None, priority: str | None = None, search: str | None = None, limit: int = 50) -> list[dict]:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM jobs ORDER BY updated_at DESC LIMIT %s", (max(1, min(limit * 5, 1000)),))
            jobs = [_deserialize(row[0]) for row in cur.fetchall()]
            jobs = [job for job in jobs if job]
    else:
        redis_conn = _redis_client()
        if redis_conn is not None:
            job_ids = redis_conn.smembers("content_forge:jobs")
            jobs = [job for job in (_deserialize(redis_conn.get(f"content_forge:job:{job_id}")) for job_id in job_ids) if job]
        else:
            with STORE.lock:
                jobs = list(STORE.jobs.values())

    filtered: list[dict] = []
    search_lower = (search or "").strip().lower()
    for job in jobs:
        if status and str(job.get("status") or "").lower() != status.lower():
            continue
        if priority and str(job.get("priority") or "").lower() != priority.lower():
            continue
        if search_lower:
            haystack = " ".join(
                str(job.get(key) or "")
                for key in ["url", "job_id", "current_step", "status", "focus_keyword_override"]
            ).lower()
            haystack += " " + str((job.get("plan") or {}).get("focus_keyword") or "").lower()
            haystack += " " + str((job.get("plan") or {}).get("title") or "").lower()
            if search_lower not in haystack:
                continue
        filtered.append(job)

    def _sort_key(item: dict) -> tuple:
        updated = item.get("updated_at")
        created = item.get("created_at")
        updated_iso = updated.isoformat() if isinstance(updated, datetime) else str(updated or "")
        created_iso = created.isoformat() if isinstance(created, datetime) else str(created or "")
        return (updated_iso, created_iso)

    filtered.sort(key=_sort_key, reverse=True)
    return filtered[: max(1, min(limit, 200))]


def add_processed_url(url_hash: str, payload: dict) -> None:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO processed_urls (url_hash, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                ON CONFLICT (url_hash) DO UPDATE SET
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (url_hash, serialize_json(payload)),
            )
        return
    redis_conn = _redis_client()
    if redis_conn is not None:
        redis_conn.set(f"content_forge:processed:{url_hash}", _serialize(payload))
        return
    with STORE.lock:
        STORE.processed_url_hashes[url_hash] = payload
        _persist_local_store()


def get_processed_url(url_hash: str) -> Optional[dict]:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM processed_urls WHERE url_hash = %s", (url_hash,))
            row = cur.fetchone()
            return _deserialize(row[0]) if row else None
    redis_conn = _redis_client()
    if redis_conn is not None:
        return _deserialize(redis_conn.get(f"content_forge:processed:{url_hash}"))
    with STORE.lock:
        return STORE.processed_url_hashes.get(url_hash)


def delete_processed_url(url_hash: str) -> bool:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("DELETE FROM processed_urls WHERE url_hash = %s", (url_hash,))
            return cur.rowcount > 0
    redis_conn = _redis_client()
    if redis_conn is not None:
        return bool(redis_conn.delete(f"content_forge:processed:{url_hash}"))
    with STORE.lock:
        if url_hash not in STORE.processed_url_hashes:
            return False
        STORE.processed_url_hashes.pop(url_hash, None)
        _persist_local_store()
        return True


def push_dlq(entry: dict) -> None:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dlq_entries (job_id, failed_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                ON CONFLICT (job_id) DO UPDATE SET
                    failed_at = NOW(),
                    data = EXCLUDED.data
                """,
                (entry["job_id"], serialize_json(entry)),
            )
            cur.execute("UPDATE job_meta SET value = value + 1 WHERE key = 'jobs_version'")
        publish_job_realtime_event(entry["job_id"], "job.failed")
        return
    redis_conn = _redis_client()
    if redis_conn is not None:
        redis_conn.lpush("content_forge:dlq", _serialize(entry))
        redis_conn.set(f"content_forge:dlq:{entry['job_id']}", _serialize(entry))
        redis_conn.incr("content_forge:jobs:version")
        publish_job_realtime_event(entry["job_id"], "job.failed")
        return
    with STORE.lock:
        STORE.dlq.appendleft(entry)
        _persist_local_store()
    _bump_jobs_version()


def get_dlq_entry(job_id: str) -> Optional[dict]:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM dlq_entries WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            return _deserialize(row[0]) if row else None
    redis_conn = _redis_client()
    if redis_conn is not None:
        return _deserialize(redis_conn.get(f"content_forge:dlq:{job_id}"))
    with STORE.lock:
        for entry in STORE.dlq:
            if entry.get("job_id") == job_id:
                return entry
    return None


def delete_dlq_entry(job_id: str) -> bool:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("DELETE FROM dlq_entries WHERE job_id = %s", (job_id,))
            deleted = cur.rowcount > 0
            if deleted:
                cur.execute("UPDATE job_meta SET value = value + 1 WHERE key = 'jobs_version'")
            return deleted
    redis_conn = _redis_client()
    if redis_conn is not None:
        key = f"content_forge:dlq:{job_id}"
        entry = redis_conn.get(key)
        if not entry:
            return False
        redis_conn.delete(key)
        redis_conn.lrem("content_forge:dlq", 0, entry)
        redis_conn.incr("content_forge:jobs:version")
        return True
    with STORE.lock:
        original_len = len(STORE.dlq)
        STORE.dlq = deque(item for item in STORE.dlq if item.get("job_id") != job_id)
        changed = len(STORE.dlq) != original_len
        if changed:
            _persist_local_store()
    if changed:
        _bump_jobs_version()
    return changed


def list_dlq() -> list[dict]:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM dlq_entries ORDER BY failed_at DESC")
            return [item for item in (_deserialize(row[0]) for row in cur.fetchall()) if item]
    redis_conn = _redis_client()
    if redis_conn is not None:
        return [item for item in (_deserialize(raw) for raw in redis_conn.lrange("content_forge:dlq", 0, -1)) if item]
    with STORE.lock:
        return list(STORE.dlq)


def stats_snapshot() -> Dict[str, Any]:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM jobs")
            jobs = [item for item in (_deserialize(row[0]) for row in cur.fetchall()) if item]
            cur.execute("SELECT COUNT(*) FROM dlq_entries")
            dlq_size = int(cur.fetchone()[0])
    else:
        redis_conn = _redis_client()
        if redis_conn is not None:
            job_ids = redis_conn.smembers("content_forge:jobs")
            jobs = [job for job in (_deserialize(redis_conn.get(f"content_forge:job:{job_id}")) for job_id in job_ids) if job]
            dlq_size = redis_conn.llen("content_forge:dlq")
        else:
            with STORE.lock:
                jobs = list(STORE.jobs.values())
                dlq_size = len(STORE.dlq)
    total = len(jobs)
    completed = [j for j in jobs if j.get("status") == "completed"]
    failed = [j for j in jobs if j.get("status") == "failed"]
    duplicate = [j for j in jobs if j.get("status") == "duplicate"]
    total_time = sum(float(j.get("metrics", {}).get("processing_time_sec", 0.0)) for j in completed)
    total_score = sum(float(j.get("qa_result", {}).get("overall_score", 0.0)) for j in completed)
    total_cost = sum(float(j.get("metrics", {}).get("estimated_cost_usd", 0.0)) for j in completed)
    return {
        "total": total,
        "completed": len(completed),
        "failed": len(failed),
        "duplicate": len(duplicate),
        "avg_time": (total_time / len(completed)) if completed else 0.0,
        "avg_score": (total_score / len(completed)) if completed else 0.0,
        "avg_cost": (total_cost / len(completed)) if completed else 0.0,
        "dlq_size": dlq_size,
    }
