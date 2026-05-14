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
        if "*" in cleaned or "%2a" in cleaned.lower():
            continue
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        gallery.append(cleaned)
        if len(gallery) >= limit:
            break
    return gallery


def _unique_gallery_items(items: list[dict], limit: int = 8) -> list[dict]:
    gallery: list[dict] = []
    seen: set[str] = set()
    for item in items:
        url = str(item.get("url") or "").strip()
        if "*" in url or "%2a" in url.lower():
            continue
        if not url or url in seen:
            continue
        seen.add(url)
        gallery.append(item)
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
        "gallery_items": _unique_gallery_items(
            [
                {
                    "url": item.get("urls", {}).get("regular") or item.get("urls", {}).get("full") or "",
                    "alt": item.get("alt_description") or item.get("description") or "",
                    "photographer": item.get("user", {}).get("name", "Unsplash"),
                }
                for item in photos
            ],
            limit=5,
        ),
        "unsplash_link": best.get("links", {}).get("html", ""),
    }


def _pexels_search(query: str, limit: int = 6) -> list[dict]:
    settings = get_settings()
    if not settings.pexels_api_key or httpx is None:
        return []
    try:
        response = httpx.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": limit, "orientation": "landscape", "locale": "vi-VN"},
            headers={"Authorization": settings.pexels_api_key},
            timeout=12,
        )
        response.raise_for_status()
    except Exception:
        return []
    return response.json().get("photos", [])


def _select_best_pexels(query: str) -> dict:
    photos = _pexels_search(query, limit=6)
    if not photos:
        return {}
    best = max(photos, key=lambda item: int(item.get("width") or 0))
    description = (best.get("alt") or "featured image").strip()
    photographer = str(best.get("photographer") or "Pexels").strip()
    return {
        "url": (best.get("src") or {}).get("large2x") or (best.get("src") or {}).get("large") or (best.get("src") or {}).get("original") or "",
        "media_id": 0,
        "alt_text": f"{query} - {description}",
        "photographer": photographer,
        "gallery": _unique_urls(
            [
                (item.get("src") or {}).get("large2x") or (item.get("src") or {}).get("large") or (item.get("src") or {}).get("original") or ""
                for item in photos
            ],
            limit=6,
        ),
        "gallery_items": _unique_gallery_items(
            [
                {
                    "url": (item.get("src") or {}).get("large2x") or (item.get("src") or {}).get("large") or (item.get("src") or {}).get("original") or "",
                    "alt": item.get("alt") or "",
                    "photographer": item.get("photographer") or "Pexels",
                }
                for item in photos
            ],
            limit=6,
        ),
        "pexels_link": best.get("url", ""),
        "unsplash_link": "",
    }


def _pexels_gallery_items(query: str, limit: int = 2) -> list[dict]:
    photos = _pexels_search(query, limit=limit)
    return _unique_gallery_items(
        [
            {
                "url": (item.get("src") or {}).get("large2x") or (item.get("src") or {}).get("large") or (item.get("src") or {}).get("original") or "",
                "alt": item.get("alt") or query,
                "photographer": item.get("photographer") or "Pexels",
            }
            for item in photos
        ],
        limit=limit,
    )


def run(
    focus_keyword: str,
    article_type: str,
    source_image_url: str | None = None,
    source_image_alt: str | None = None,
    source_image_urls: list[str] | None = None,
    provider: str = "",
    section_queries: list[str] | None = None,
) -> dict:
    query = focus_keyword or article_type or "trà"
    if provider == "pexels":
        pexels = _select_best_pexels(query)
        if pexels.get("url"):
            section_items: list[dict] = []
            for section_query in (section_queries or [])[:4]:
                section_query = str(section_query or "").strip()
                if section_query:
                    section_items.extend(_pexels_gallery_items(f"{section_query} {query}", limit=2))
            if section_items:
                gallery_items = _unique_gallery_items(section_items + list(pexels.get("gallery_items") or []), limit=8)
                pexels["gallery_items"] = gallery_items
                pexels["gallery"] = [item["url"] for item in gallery_items]
            return pexels

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
            "gallery_items": [],
            "unsplash_link": "",
        }
    alt_text = query if not source_image_alt else f"{query} - {source_image_alt}"
    return {
        "url": gallery[0],
        "media_id": 0,
        "alt_text": alt_text,
        "photographer": "Source gallery",
        "gallery": gallery[:8],
        "gallery_items": [{"url": url, "alt": alt_text, "photographer": "Source gallery"} for url in gallery[:8]],
        "unsplash_link": "",
    }
