from __future__ import annotations

import re
from urllib.parse import urlparse

from app.search import google_search


GENERIC_OUTBOUND_BY_TOKEN = {
    "trà": "https://vi.wikipedia.org/wiki/Tr%C3%A0",
    "thuy tinh": "https://vi.wikipedia.org/wiki/Th%E1%BB%A7y_tinh",
    "thủy tinh": "https://vi.wikipedia.org/wiki/Th%E1%BB%A7y_tinh",
}


def run(
    html: str,
    focus_keyword: str,
    source_url: str | None = None,
    additional_sources: list[dict] | None = None,
    current_title: str | None = None,
    site_profile: dict | None = None,
) -> str:
    query_text = " ".join(part for part in [current_title or "", focus_keyword] if part).strip() or focus_keyword
    base = str((site_profile or {}).get("url") or "").rstrip("/")

    current_title_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.IGNORECASE | re.DOTALL)
    current_title = (current_title or "").strip()
    if not current_title and current_title_match:
        current_title = re.sub(r"<[^>]+>", "", current_title_match.group(1)).strip()

    if base and f'href="{base}' not in html:
        html += f'\n<p><a href="{base}/?post_type=product">Xem thêm các sản phẩm cùng nhóm</a></p>'

    outbound_url = ""
    for item in additional_sources or []:
        candidate = (item or {}).get("url", "")
        if not candidate:
            continue
        if source_url and urlparse(candidate).netloc == urlparse(source_url).netloc:
            continue
        outbound_url = candidate
        break
    if not outbound_url and focus_keyword:
        for item in google_search(focus_keyword, num=3):
            candidate = item.get("url", "")
            if not candidate:
                continue
            if source_url and urlparse(candidate).netloc == urlparse(source_url).netloc:
                continue
            outbound_url = candidate
            break
    if not outbound_url and query_text and query_text != focus_keyword:
        for item in google_search(query_text, num=3):
            candidate = item.get("url", "")
            if not candidate:
                continue
            if source_url and urlparse(candidate).netloc == urlparse(source_url).netloc:
                continue
            outbound_url = candidate
            break
    if not outbound_url:
        lowered = f"{current_title} {focus_keyword}".lower()
        for token, candidate in GENERIC_OUTBOUND_BY_TOKEN.items():
            if token in lowered:
                outbound_url = candidate
                break
    if outbound_url and outbound_url not in html:
        html += f'\n<p><a href="{outbound_url}" target="_blank" rel="noopener">Đọc thêm thông tin liên quan</a></p>'
    return html
