from __future__ import annotations

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from app.agents import (
    deduplicator,
    enricher,
    extractor,
    fetcher,
    humanizer,
    image_selector,
    internal_linker,
    knowledge,
    media_uploader,
    planner,
    qa,
    writer,
)


def summarize(value):
    if isinstance(value, str):
        return {"type": "str", "len": len(value)}
    if isinstance(value, list):
        return {"type": "list", "len": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(value.keys())}
    return {"type": type(value).__name__}


def timed(name, fn):
    started = time.time()
    value = fn()
    elapsed = round(time.time() - started, 2)
    print(json.dumps({"step": name, "elapsed_sec": elapsed, "summary": summarize(value)}, ensure_ascii=False))
    return value


def main() -> None:
    load_dotenv()
    url = "https://loctancuong.vn/sp-tra-moc-cau-hh/"
    state: dict = {
        "job_id": "debug-loctancuong-tra-moc-cau-hh",
        "source_url": url,
        "source_type": "product",
    }

    state.update(timed("deduplicator", lambda: deduplicator.run(url)))
    fetched = timed("fetcher", lambda: fetcher.run(url))
    state.update(fetched)
    state["fetch_result"] = fetched
    extracted = timed(
        "extractor",
        lambda: extractor.run(state.get("clean_content", ""), state.get("metadata")),
    )
    state["extracted"] = extracted
    state["title"] = extracted.get("title") or state.get("title")
    state["key_points"] = extracted.get("important_facts", [])
    state["focus_keyword"] = extracted.get("focus_keyword") or state.get("focus_keyword")
    facts = timed(
        "knowledge",
        lambda: knowledge.run(
            state.get("key_points", []),
            metadata={**(state.get("metadata") or {}), "title": state.get("fetch_result", {}).get("title")},
            extracted=state.get("extracted"),
        ),
    )
    state["knowledge_facts"] = facts
    additional = timed(
        "enricher",
        lambda: enricher.run(state.get("key_points", []), state.get("metadata", {}).get("focus_keyword")),
    )
    state["additional_sources"] = additional
    plan = timed(
        "planner",
        lambda: planner.run(
            state.get("key_points", []),
            state.get("knowledge_facts", []),
            state.get("metadata", {}),
            state.get("focus_keyword"),
            state.get("extracted"),
        ),
    )
    state["plan"] = plan
    state["focus_keyword"] = plan.get("focus_keyword") or state.get("focus_keyword")
    image_data = timed(
        "image_selector",
        lambda: image_selector.run(
            state.get("focus_keyword"),
            state.get("plan", {}).get("article_type"),
            source_image_url=state.get("metadata", {}).get("featured_image_url"),
            source_image_alt=state.get("metadata", {}).get("featured_image_alt"),
            source_image_urls=state.get("metadata", {}).get("image_urls"),
        ),
    )
    state["image_data"] = image_data
    upload_data = timed("media_uploader", lambda: media_uploader.run(image_data))
    state["image_data"] = {**image_data, **upload_data}
    prewriter = {
        "title": state.get("title"),
        "focus_keyword": state.get("focus_keyword"),
        "metadata": state.get("metadata"),
        "clean_content_len": len(state.get("clean_content") or ""),
        "clean_content_preview": (state.get("clean_content") or "")[:2600],
        "extracted": state.get("extracted"),
        "plan": state.get("plan"),
        "image_data": state.get("image_data"),
    }
    prewriter_path = Path("tmp/debug_loctancuong_prewriter.json")
    prewriter_path.parent.mkdir(parents=True, exist_ok=True)
    prewriter_path.write_text(json.dumps(prewriter, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"saved": str(prewriter_path)}, ensure_ascii=False))
    if os.getenv("STOP_AFTER_PREWRITER") == "1":
        return
    state["draft"] = timed("writer", lambda: writer.run(state))
    humanized = timed(
        "humanizer",
        lambda: humanizer.run(state.get("draft", {}).get("html", ""), state.get("source_type")),
    )
    state["humanized_html"] = humanized.get("html", "")
    state["linked_html"] = timed(
        "internal_linker",
        lambda: internal_linker.run(
            state.get("humanized_html", ""),
            state.get("focus_keyword", ""),
            source_url=state.get("source_url"),
            additional_sources=state.get("additional_sources"),
            current_title=state.get("title"),
        ),
    )
    qa_result = timed("qa", lambda: qa.run(state))
    state["qa_result"] = qa_result

    output = {
        "title": state.get("title"),
        "focus_keyword": state.get("focus_keyword"),
        "meta_title": state.get("plan", {}).get("meta_title"),
        "outline": state.get("plan", {}).get("outline"),
        "html_len": len(state.get("linked_html") or ""),
        "linked_html": state.get("linked_html"),
        "qa_result": qa_result,
        "key_points": state.get("key_points"),
        "metadata": state.get("metadata"),
        "extracted": state.get("extracted"),
    }
    out_path = Path("tmp/debug_loctancuong.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"saved": str(out_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
