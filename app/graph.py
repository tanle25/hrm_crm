from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from typing import Callable

from app.agents import deduplicator, enricher, extractor, fetcher, humanizer, image_selector, internal_linker, knowledge, media_uploader, planner, publisher, qa, seo_adjuster, writer
from app.job_store import add_processed_url, delete_dlq_entry, get_dlq_entry, get_job
from app.logging import get_logger
from app.metrics import active_jobs, job_duration, jobs_completed, jobs_duplicate, jobs_failed
from app.queue import enqueue_saved_state, send_to_dlq, update_job
from app.webhook import send_job_webhook

try:
    from langgraph.graph import END, StateGraph
except ImportError:  # pragma: no cover
    END = "__end__"
    StateGraph = None


PIPELINE_STEPS = [
    "deduplicator",
    "fetcher",
    "extractor",
    "knowledge",
    "enricher",
    "planner",
    "image_selector",
    "media_uploader",
    "writer",
    "humanizer",
    "internal_linker",
    "qa",
    "seo_adjuster",
    "publisher",
]

log = get_logger("content_forge.graph")


def _mark_step(state: dict, job_id: str, step: str) -> None:
    state["current_step"] = step
    state["status"] = "processing"
    state["updated_at"] = datetime.utcnow().isoformat()
    update_job(job_id, state)
    log.info("step_started", job_id=job_id, step=step, url=state.get("url"))


def _update_metrics(state: dict, step: str, tokens_delta: int = 0, cost_delta: float = 0.0) -> None:
    metrics = state.setdefault("metrics", {})
    metrics.setdefault("agents_used", [])
    if step not in metrics["agents_used"]:
        metrics["agents_used"].append(step)
    metrics["total_tokens_used"] = int(metrics.get("total_tokens_used", 0) + tokens_delta)
    metrics["estimated_cost_usd"] = round(float(metrics.get("estimated_cost_usd", 0.0) + cost_delta), 4)


def _node(step: str, handler: Callable[[dict], dict]) -> Callable[[dict], dict]:
    def run(state: dict) -> dict:
        job_id = state["_job_id"]
        _mark_step(state, job_id, step)
        started = time.perf_counter()
        timing = {
            "started_at": datetime.utcnow().isoformat(),
            "status": "running",
        }
        try:
            result = handler(state)
            timing["status"] = "completed"
            log.info(
                "step_completed",
                job_id=job_id,
                step=step,
                duration_sec=round(time.perf_counter() - started, 2),
                status="success",
            )
            return result
        except Exception as exc:
            timing["status"] = "failed"
            timing["error"] = str(exc)
            log.error(
                "step_failed",
                job_id=job_id,
                step=step,
                duration_sec=round(time.perf_counter() - started, 2),
                error=str(exc),
            )
            raise
        finally:
            timing["ended_at"] = datetime.utcnow().isoformat()
            timing["duration_sec"] = round(time.perf_counter() - started, 2)
            state.setdefault("step_timings", {}).setdefault(step, []).append(timing)
            state["updated_at"] = datetime.utcnow().isoformat()
            update_job(job_id, state)

    return run


def _run_deduplicator(state: dict) -> dict:
    state["dedup_result"] = deduplicator.run(state["url"])
    return state


def _run_fetcher(state: dict) -> dict:
    if state.get("source_origin") == "shopee" and state.get("source_seed"):
        state["fetch_result"] = fetcher.run_seeded_product(state["source_seed"])
    else:
        state["fetch_result"] = fetcher.run(state["url"])
    return state


def _run_extractor(state: dict) -> dict:
    state["extracted"] = extractor.run(
        state["fetch_result"]["clean_content"],
        state["fetch_result"].get("metadata", {}),
    )
    _update_metrics(state, "extractor", tokens_delta=600, cost_delta=0.001)
    return state


def _run_knowledge(state: dict) -> dict:
    metadata = dict(state["fetch_result"].get("metadata", {}))
    metadata["title"] = state["fetch_result"].get("title")
    state["knowledge_facts"] = knowledge.run(
        state["extracted"]["key_points"],
        metadata=metadata,
        extracted=state["extracted"],
    )
    _update_metrics(state, "knowledge", tokens_delta=200, cost_delta=0.0005)
    return state


def _run_enricher(state: dict) -> dict:
    state["additional_sources"] = enricher.run(state["extracted"]["key_points"], state.get("focus_keyword_override"))
    return state


def _run_planner(state: dict) -> dict:
    metadata = dict(state["fetch_result"]["metadata"])
    metadata["title"] = state["fetch_result"]["title"]
    state["plan"] = planner.run(
        key_points=state["extracted"]["key_points"],
        knowledge_facts=state["knowledge_facts"],
        metadata=metadata,
        focus_keyword_override=state.get("focus_keyword_override"),
        extracted=state["extracted"],
    )
    _update_metrics(state, "planner", tokens_delta=900, cost_delta=0.002)
    return state


def _run_writer(state: dict) -> dict:
    state["draft"] = writer.run(state)
    _update_metrics(state, "writer", tokens_delta=5000, cost_delta=0.021)
    return state


def _run_image_selector(state: dict) -> dict:
    metadata = state["fetch_result"].get("metadata", {})
    state["image_data"] = image_selector.run(
        state["plan"]["focus_keyword"],
        state["plan"]["article_type"],
        metadata.get("featured_image_url"),
        metadata.get("featured_image_alt"),
        metadata.get("image_urls") or [],
    )
    return state


def _run_media_uploader(state: dict) -> dict:
    state["image_data"] = media_uploader.run(state.get("image_data") or {})
    return state


def _run_humanizer(state: dict) -> dict:
    state["humanized"] = humanizer.run(
        state["draft"]["html"],
        state["fetch_result"].get("metadata", {}).get("source_type"),
        state.get("site_profile") or {},
        state.get("content_mode"),
    )
    state["humanized"]["html"] = writer._inject_inline_images(
        state["humanized"].get("html") or "",
        writer._image_library(state),
        state["plan"]["focus_keyword"],
    )
    _update_metrics(state, "humanizer", tokens_delta=3500, cost_delta=0.028)
    return state


def _run_internal_linker(state: dict) -> dict:
    state["linked_html"] = internal_linker.run(
        state["humanized"]["html"],
        state["plan"]["focus_keyword"],
        state["fetch_result"].get("metadata", {}).get("url"),
        state.get("additional_sources"),
        state.get("plan", {}).get("title"),
        state.get("site_profile") or {},
    )
    state["linked_html"] = writer._inject_inline_images(
        state["linked_html"],
        writer._image_library(state),
        state["plan"]["focus_keyword"],
    )
    return state


def _run_qa(state: dict) -> dict:
    state["qa_result"] = qa.run(state)
    _update_metrics(state, "qa", tokens_delta=4800, cost_delta=0.018)
    return state


def _run_seo_adjuster(state: dict) -> dict:
    return seo_adjuster.run(state)


def _run_qa_failed(state: dict) -> dict:
    raise RuntimeError("QA failed after retries")


def _run_publisher(state: dict) -> dict:
    if str(state.get("source_origin") or "").strip().lower() == "shopee":
        published = publisher.run_shopee(state)
    else:
        published = publisher.run(state)
    state.update(published)
    add_processed_url(
        state["dedup_result"]["url_hash"],
        {"url": state["url"], "woo_post_id": state["woo_post_id"], "woo_link": state["woo_link"]},
    )
    return state


def _hydrate_child_from_parent(state: dict) -> dict:
    parent_id = state.get("parent_job_id")
    if not parent_id:
        raise RuntimeError("Shared publish child is missing parent_job_id")
    parent = get_job(parent_id)
    if not parent:
        raise RuntimeError("Shared content master job not found")
    if parent.get("status") != "completed":
        raise RuntimeError("Shared content master job is not completed yet")
    for key in ["dedup_result", "fetch_result", "extracted", "knowledge_facts", "additional_sources", "plan", "draft", "humanized", "image_data", "metrics"]:
        if key in parent:
            state[key] = deepcopy(parent[key])
    state["linked_html"] = ""
    state["final_article"] = {}
    state["woo_post_id"] = None
    state["woo_link"] = None
    state["error"] = None
    return state


def _run_shared_master_pipeline(state: dict) -> dict:
    for step, handler in [
        ("deduplicator", _run_deduplicator),
        ("fetcher", _run_fetcher),
        ("extractor", _run_extractor),
        ("knowledge", _run_knowledge),
        ("enricher", _run_enricher),
        ("planner", _run_planner),
        ("image_selector", _run_image_selector),
        ("media_uploader", _run_media_uploader),
        ("writer", _run_writer),
        ("humanizer", _run_humanizer),
    ]:
        state = _node(step, handler)(state)
        if step == "deduplicator" and state["dedup_result"]["is_duplicate"]:
            return state
    state["linked_html"] = state["humanized"]["html"]
    state = _node("qa", _run_qa)(state)
    if not state.get("qa_result", {}).get("pass"):
        route = _route_qa(state)
        if route == "writer":
            state = _node("writer", _run_writer)(state)
            state = _node("humanizer", _run_humanizer)(state)
            state["linked_html"] = state["humanized"]["html"]
            state = _node("qa", _run_qa)(state)
        elif route == "humanizer":
            state = _node("humanizer", _run_humanizer)(state)
            state["linked_html"] = state["humanized"]["html"]
            state = _node("qa", _run_qa)(state)
        elif route == "seo_adjuster":
            state = _node("seo_adjuster", _run_seo_adjuster)(state)
            state = _node("qa", _run_qa)(state)
        if not state.get("qa_result", {}).get("pass"):
            raise RuntimeError("Shared master QA failed after retries")
    return state


def _run_shared_publish_child_pipeline(state: dict) -> dict:
    state = _hydrate_child_from_parent(state)
    state = _node("internal_linker", _run_internal_linker)(state)
    state = _node("qa", _run_qa)(state)
    if not state.get("qa_result", {}).get("pass"):
        state = _node("seo_adjuster", _run_seo_adjuster)(state)
        state = _node("qa", _run_qa)(state)
    if not state.get("qa_result", {}).get("pass"):
        raise RuntimeError("Shared publish child QA failed")
    state = _node("publisher", _run_publisher)(state)
    return state


def _enqueue_shared_children(state: dict) -> None:
    for child_id in state.get("child_job_ids") or []:
        child_state = get_job(child_id)
        if not child_state:
            continue
        queue_name = enqueue_saved_state(child_id, child_state)
        if queue_name == "inline":
            run_pipeline(child_id, child_state)


def _route_deduplicator(state: dict) -> str:
    return "duplicate" if state["dedup_result"]["is_duplicate"] else "fetcher"


def _route_qa(state: dict) -> str:
    qa_result = state.get("qa_result", {})
    if qa_result.get("pass"):
        return "publisher"
    retry_count = int(qa_result.get("retry_count", 0))
    if retry_count >= 2:
        return "qa_failed"
    retry_target = qa_result.get("feedback", {}).get("retry_target", "writer")
    if retry_target == "seo_adjuster":
        return "seo_adjuster"
    return "humanizer" if retry_target == "humanizer" else "writer"


@lru_cache(maxsize=1)
def _get_langgraph_app():
    if StateGraph is None:
        return None
    graph = StateGraph(dict)
    graph.add_node("deduplicator", _node("deduplicator", _run_deduplicator))
    graph.add_node("fetcher", _node("fetcher", _run_fetcher))
    graph.add_node("extractor", _node("extractor", _run_extractor))
    graph.add_node("knowledge", _node("knowledge", _run_knowledge))
    graph.add_node("enricher", _node("enricher", _run_enricher))
    graph.add_node("planner", _node("planner", _run_planner))
    graph.add_node("image_selector", _node("image_selector", _run_image_selector))
    graph.add_node("media_uploader", _node("media_uploader", _run_media_uploader))
    graph.add_node("writer", _node("writer", _run_writer))
    graph.add_node("humanizer", _node("humanizer", _run_humanizer))
    graph.add_node("internal_linker", _node("internal_linker", _run_internal_linker))
    graph.add_node("qa", _node("qa", _run_qa))
    graph.add_node("seo_adjuster", _node("seo_adjuster", _run_seo_adjuster))
    graph.add_node("qa_failed", _node("qa", _run_qa_failed))
    graph.add_node("publisher", _node("publisher", _run_publisher))

    graph.set_entry_point("deduplicator")
    graph.add_conditional_edges("deduplicator", _route_deduplicator, {"duplicate": END, "fetcher": "fetcher"})
    graph.add_edge("fetcher", "extractor")
    graph.add_edge("extractor", "knowledge")
    graph.add_edge("knowledge", "enricher")
    graph.add_edge("enricher", "planner")
    graph.add_edge("planner", "image_selector")
    graph.add_edge("image_selector", "media_uploader")
    graph.add_edge("media_uploader", "writer")
    graph.add_edge("writer", "humanizer")
    graph.add_edge("humanizer", "internal_linker")
    graph.add_edge("internal_linker", "qa")
    graph.add_conditional_edges(
        "qa",
        _route_qa,
        {
            "writer": "writer",
            "humanizer": "humanizer",
            "seo_adjuster": "seo_adjuster",
            "publisher": "publisher",
            "qa_failed": "qa_failed",
        },
    )
    graph.add_edge("seo_adjuster", "qa")
    graph.add_edge("qa_failed", END)
    graph.add_edge("publisher", END)
    return graph.compile()


def _run_pipeline_sequential(state: dict) -> dict:
    for step in PIPELINE_STEPS:
        if step == "deduplicator":
            state = _node(step, _run_deduplicator)(state)
            if state["dedup_result"]["is_duplicate"]:
                return state
            continue
        if step == "fetcher":
            state = _node(step, _run_fetcher)(state)
            continue
        if step == "extractor":
            state = _node(step, _run_extractor)(state)
            continue
        if step == "knowledge":
            state = _node(step, _run_knowledge)(state)
            continue
        if step == "enricher":
            state = _node(step, _run_enricher)(state)
            continue
        if step == "planner":
            state = _node(step, _run_planner)(state)
            continue
        if step == "image_selector":
            state = _node(step, _run_image_selector)(state)
            continue
        if step == "media_uploader":
            state = _node(step, _run_media_uploader)(state)
            continue
        if step == "writer":
            state = _node(step, _run_writer)(state)
            continue
        if step == "humanizer":
            state = _node(step, _run_humanizer)(state)
            continue
        if step == "internal_linker":
            state = _node(step, _run_internal_linker)(state)
            continue
        if step == "qa":
            state = _node(step, _run_qa)(state)
            route = _route_qa(state)
            if route == "writer":
                state = _node("writer", _run_writer)(state)
                state = _node("humanizer", _run_humanizer)(state)
                state = _node("internal_linker", _run_internal_linker)(state)
                state = _node("qa", _run_qa)(state)
                route = _route_qa(state)
            elif route == "humanizer":
                state = _node("humanizer", _run_humanizer)(state)
                state = _node("internal_linker", _run_internal_linker)(state)
                state = _node("qa", _run_qa)(state)
                route = _route_qa(state)
            elif route == "seo_adjuster":
                state = _node("seo_adjuster", _run_seo_adjuster)(state)
                state = _node("qa", _run_qa)(state)
                route = _route_qa(state)
            if route == "qa_failed":
                raise RuntimeError("QA failed after retries")
            continue
        if step == "publisher":
            state = _node(step, _run_publisher)(state)
            continue
    return state


def run_pipeline(job_id: str, payload: dict) -> dict:
    state = deepcopy(payload)
    state["_job_id"] = job_id
    workflow_role = str(state.get("workflow_role") or "standard")
    start = time.perf_counter()
    active_jobs.inc()
    log.info("job_started", job_id=job_id, url=state.get("url"), priority=state.get("priority"), workflow_role=workflow_role)
    try:
        if workflow_role == "shared_master":
            state = _run_shared_master_pipeline(state)
        elif workflow_role == "shared_publish_child":
            state = _run_shared_publish_child_pipeline(state)
        else:
            graph_app = _get_langgraph_app()
            state = graph_app.invoke(state) if graph_app is not None else _run_pipeline_sequential(state)

        if state.get("dedup_result", {}).get("is_duplicate"):
            state["status"] = "duplicate"
            jobs_duplicate.inc()
        else:
            state["status"] = "completed"
            jobs_completed.inc()
        if workflow_role == "shared_master" and state.get("status") == "completed":
            _enqueue_shared_children(state)
        duration = time.perf_counter() - start
        state.setdefault("metrics", {})["processing_time_sec"] = round(duration, 2)
        state["updated_at"] = datetime.utcnow().isoformat()
        state.pop("_job_id", None)
        job_duration.observe(duration)
        update_job(job_id, state)
        send_job_webhook(job_id, state, "job.completed" if state.get("status") == "completed" else "job.duplicate")
        log.info(
            "job_finished",
            job_id=job_id,
            status=state.get("status"),
            duration_sec=round(duration, 2),
            woo_post_id=state.get("woo_post_id"),
            qa_score=state.get("qa_result", {}).get("overall_score"),
        )
        return state
    except Exception as exc:
        duration = time.perf_counter() - start
        state["status"] = "failed"
        state["error"] = str(exc)
        state.setdefault("metrics", {})["processing_time_sec"] = round(duration, 2)
        state["updated_at"] = datetime.utcnow().isoformat()
        state.pop("_job_id", None)
        jobs_failed.labels(reason="pipeline_error").inc()
        send_to_dlq(job_id, state, str(exc))
        update_job(job_id, state)
        send_job_webhook(job_id, state, "job.failed")
        log.error(
            "job_failed",
            job_id=job_id,
            status=state.get("status"),
            duration_sec=round(duration, 2),
            error=str(exc),
        )
        return state
    finally:
        active_jobs.dec()


async def run_pipeline_async(job_id: str, payload: dict) -> dict:
    return await asyncio.to_thread(run_pipeline, job_id, payload)


def retry_from_dlq(job_id: str) -> dict | None:
    entry = get_dlq_entry(job_id)
    if not entry:
        return None
    payload = deepcopy(entry.get("state") or get_job(job_id))
    if not payload:
        return None
    payload["status"] = "pending"
    payload["error"] = None
    payload["qa_result"]["retry_count"] = 0
    update_job(job_id, payload)
    delete_dlq_entry(job_id)
    return run_pipeline(job_id, payload)
