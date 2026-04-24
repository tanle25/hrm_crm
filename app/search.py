from __future__ import annotations

import html
import re
from urllib.parse import quote_plus

from app.config import get_settings

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


def strip_html(value: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", value or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def google_search(query: str, num: int = 2) -> list[dict]:
    settings = get_settings()
    if httpx is None:
        return []
    if settings.google_search_api_key and settings.google_search_engine_id:
        try:
            response = httpx.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": settings.google_search_api_key,
                    "cx": settings.google_search_engine_id,
                    "q": query,
                    "num": max(1, min(num, 10)),
                },
                timeout=15,
            )
            response.raise_for_status()
            items = response.json().get("items", [])
            return [
                {
                    "url": item.get("link", ""),
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "source": "google_search",
                }
                for item in items
                if item.get("link")
            ][:num]
        except Exception:
            return []
    return _duckduckgo_search(query, num=num)


def fetch_page_summary(url: str, limit: int = 700) -> str:
    if httpx is None:
        return ""
    try:
        response = httpx.get(url, timeout=12, follow_redirects=True, headers={"User-Agent": "ContentForge/2.0"})
        response.raise_for_status()
    except Exception:
        return ""
    return strip_html(response.text)[:limit]


def _extract_result_urls(search_html: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a[^>]+href="(https?://[^"]+)"', search_html, re.IGNORECASE):
        url = html.unescape(match.group(1))
        if any(blocked in url for blocked in ["duckduckgo.com", "google.com", "bing.com", "facebook.com", "youtube.com"]):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
        if len(urls) >= 6:
            break
    return urls


def _duckduckgo_search(query: str, num: int) -> list[dict]:
    if httpx is None:
        return []
    search_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        response = httpx.get(search_url, timeout=12, follow_redirects=True, headers={"User-Agent": "ContentForge/2.0"})
        response.raise_for_status()
    except Exception:
        return []
    return [{"url": url, "title": "", "snippet": "", "source": "duckduckgo"} for url in _extract_result_urls(response.text)[:num]]
