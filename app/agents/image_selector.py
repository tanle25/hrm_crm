from __future__ import annotations

from app.config import get_settings

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


def _unique_urls(urls: list[str], limit: int = 8) -> list[str]:
    gallery: list[str] = []
    seen: set[str] = set()
    for url in urls:
        cleaned = (url or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        gallery.append(cleaned)
        if len(gallery) >= limit:
            break
    return gallery


def _unsplash_search(query: str, limit: int = 5) -> list[dict]:
    settings = get_settings()
    if not settings.unsplash_access_key or httpx is None:
        return []
    try:
        response = httpx.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": limit, "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {settings.unsplash_access_key}"},
            timeout=12,
        )
        response.raise_for_status()
    except Exception:
        return []
    return response.json().get("results", [])


def _select_best_unsplash(query: str) -> dict:
    photos = _unsplash_search(query, limit=5)
    if not photos:
        return {}
    best = max(photos, key=lambda item: int(item.get("width") or 0))
    description = (best.get("alt_description") or best.get("description") or "featured image").strip()
    return {
        "url": best.get("urls", {}).get("regular") or best.get("urls", {}).get("full") or "",
        "media_id": 0,
        "alt_text": f"{query} - {description}",
        "photographer": best.get("user", {}).get("name", "Unsplash"),
        "gallery": _unique_urls(
            [
                item.get("urls", {}).get("regular") or item.get("urls", {}).get("full") or ""
                for item in photos
            ],
            limit=5,
        ),
        "unsplash_link": best.get("links", {}).get("html", ""),
    }


def run(
    focus_keyword: str,
    article_type: str,
    source_image_url: str | None = None,
    source_image_alt: str | None = None,
    source_image_urls: list[str] | None = None,
) -> dict:
    query = focus_keyword or article_type or "trà"
    unsplash = _select_best_unsplash(query)
    if unsplash.get("url"):
        return unsplash

    source_urls = list(source_image_urls or [])
    if source_image_url:
        source_urls.insert(0, source_image_url)
    gallery = _unique_urls(source_urls)
    if not gallery:
        return {
            "url": "",
            "media_id": 0,
            "alt_text": "",
            "photographer": "",
            "gallery": [],
            "unsplash_link": "",
        }
    alt_text = query if not source_image_alt else f"{query} - {source_image_alt}"
    return {
        "url": gallery[0],
        "media_id": 0,
        "alt_text": alt_text,
        "photographer": "Source gallery",
        "gallery": gallery[:8],
        "unsplash_link": "",
    }
