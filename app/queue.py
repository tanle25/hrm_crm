from __future__ import annotations

import uuid
from typing import Optional

from app.config import get_settings
from app.dlq import handle_failed_job
from app.job_store import save_job, stats_snapshot
from app.schemas import PipelineState

try:
    from redis import Redis
    from rq import Queue
    from rq.job import Job
    from rq.retry import Retry
except ImportError:  # pragma: no cover
    Redis = None
    Queue = None
    Job = None
    Retry = None


def _get_rq_queue(priority: str):
    settings = get_settings()
    if Queue is None or Redis is None:
        return None
    redis_conn = Redis.from_url(settings.redis_url)
    queue_name = "high_priority" if priority == "high" else "content_pipeline"
    return Queue(queue_name, connection=redis_conn)


def _get_dlq_queue():
    settings = get_settings()
    if Queue is None or Redis is None:
        return None
    redis_conn = Redis.from_url(settings.redis_url)
    return Queue("dlq", connection=redis_conn)


def create_job_id() -> str:
    return uuid.uuid4().hex


def init_job_state(job_id: str, payload: PipelineState) -> None:
    state = payload.model_dump(by_alias=True)
    state["job_id"] = job_id
    save_job(job_id, state)


def enqueue_job(job_id: str, payload: PipelineState) -> str:
    settings = get_settings()
    if settings.queue_mode == "rq":
        queue = _get_rq_queue(payload.priority)
        if queue is not None:
            queue.enqueue(
                "app.graph.run_pipeline",
                kwargs={"job_id": job_id, "payload": payload.model_dump(by_alias=True)},
                job_timeout=settings.processing_timeout_sec,
                retry=Retry(max=2, intervals=[60, 120]) if Retry is not None else None,
                result_ttl=86400,
                failure_ttl=604800,
            )
            return queue.name
    return "inline"


def enqueue_saved_state(job_id: str, state: dict) -> str:
    settings = get_settings()
    priority = str(state.get("priority") or "normal")
    if settings.queue_mode == "rq":
        queue = _get_rq_queue(priority)
        if queue is not None:
            queue.enqueue(
                "app.graph.run_pipeline",
                kwargs={"job_id": job_id, "payload": state},
                job_timeout=settings.processing_timeout_sec,
                retry=Retry(max=2, intervals=[60, 120]) if Retry is not None else None,
                result_ttl=86400,
                failure_ttl=604800,
            )
            return queue.name
    return "inline"


def update_job(job_id: str, state: dict) -> None:
    save_job(job_id, state)


def send_to_dlq(job_id: str, state: dict, reason: str) -> None:
    settings = get_settings()
    if settings.queue_mode == "rq":
        queue = _get_dlq_queue()
        if queue is not None:
            queue.enqueue(
                "app.dlq.handle_failed_job",
                kwargs={"job_id": job_id, "state": state, "reason": reason},
                result_ttl=2592000,
            )
            return
    handle_failed_job(job_id, state, reason)


def queue_is_full() -> bool:
    snapshot = stats_snapshot()
    return snapshot["total"] >= get_settings().max_queue_size
