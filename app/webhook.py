from __future__ import annotations

from copy import deepcopy

import httpx

from app.config import get_settings
from app.logging import get_logger

log = get_logger("content_forge.webhook")


def _payload(job_id: str, state: dict, event: str) -> dict:
    qa_result = state.get("qa_result") or {}
    metrics = state.get("metrics") or {}
    payload = {
        "event": event,
        "job_id": job_id,
        "status": state.get("status"),
        "url": state.get("url"),
        "current_step": state.get("current_step"),
        "woo_post_id": state.get("woo_post_id"),
        "woo_link": state.get("woo_link"),
        "qa_score": qa_result.get("overall_score"),
        "qa_pass": qa_result.get("pass"),
        "error": state.get("error"),
        "processing_time_sec": metrics.get("processing_time_sec"),
        "tokens_used": metrics.get("total_tokens_used"),
        "estimated_cost_usd": metrics.get("estimated_cost_usd"),
        "step_timings": deepcopy(state.get("step_timings") or {}),
    }
    if state.get("forced_publish"):
        payload["forced_publish"] = True
        payload["forced_publish_reason"] = state.get("forced_publish_reason")
    if state.get("seo_adjustments"):
        payload["seo_adjustments"] = deepcopy(state.get("seo_adjustments"))
    return payload


def send_job_webhook(job_id: str, state: dict, event: str) -> bool:
    settings = get_settings()
    if not settings.webhook_url:
        return False
    payload = _payload(job_id, state, event)
    try:
        response = httpx.post(settings.webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        log.info(
            "webhook_sent",
            webhook_event=event,
            job_id=job_id,
            status=state.get("status"),
            webhook_url=settings.webhook_url,
            status_code=response.status_code,
        )
        return True
    except Exception as exc:
        log.error(
            "webhook_failed",
            webhook_event=event,
            job_id=job_id,
            status=state.get("status"),
            webhook_url=settings.webhook_url,
            error=str(exc),
        )
        return False
