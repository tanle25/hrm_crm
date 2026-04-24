from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from app.agents import publisher
from app.job_store import delete_dlq_entry, get_dlq_entry, save_job
from app.logging import get_logger
from app.webhook import send_job_webhook

log = get_logger("content_forge.dlq")


def handle_failed_job(job_id: str, state: dict, reason: str) -> dict:
    entry = {
        "job_id": job_id,
        "url": state.get("url"),
        "failed_at": datetime.utcnow(),
        "reason": reason,
        "qa_score": state.get("qa_result", {}).get("overall_score", 0.0),
        "state": state,
    }
    from app.job_store import push_dlq

    push_dlq(entry)
    log.error("dlq_enqueued", job_id=job_id, reason=reason, url=state.get("url"))
    return entry


def publish_anyway(job_id: str) -> dict | None:
    entry = get_dlq_entry(job_id)
    if not entry:
        return None
    state = deepcopy(entry.get("state") or {})
    if not state.get("linked_html"):
        state["status"] = "failed"
        state["error"] = "DLQ state thiếu linked_html, không thể force publish."
        save_job(job_id, state)
        return state
    published = publisher.run(state)
    state.update(published)
    state["status"] = "completed"
    state["error"] = None
    state["forced_publish"] = True
    state["forced_publish_reason"] = entry.get("reason")
    save_job(job_id, state)
    delete_dlq_entry(job_id)
    send_job_webhook(job_id, state, "job.forced_publish")
    log.info(
        "dlq_forced_publish_completed",
        job_id=job_id,
        woo_post_id=state.get("woo_post_id"),
        reason=entry.get("reason"),
    )
    return state
