from __future__ import annotations

from app.search import fetch_page_summary, google_search


def run(key_points: list[str], focus_keyword: str | None) -> list[dict]:
    if not key_points:
        return []
    query = f"{focus_keyword or key_points[0]} {key_points[0]}"
    sources: list[dict] = []
    for item in google_search(query, num=2):
        url = item.get("url", "")
        if not url:
            continue
        summary = fetch_page_summary(url, limit=500) or (item.get("snippet") or "")
        if len(summary.split()) < 20:
            continue
        sources.append({"url": url, "summary": summary[:500]})
        if len(sources) >= 2:
            break
    return sources
