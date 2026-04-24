from __future__ import annotations

import json
import math
import re
from html import unescape
from urllib.parse import urlparse

from app.llm import call_json


SEO_ADJUSTER_SYSTEM_PROMPT = """
Bạn là SEO editor tiếng Việt cho WooCommerce.
Nhiệm vụ: chỉnh HTML hiện có thật nhẹ để đạt Rank Math tốt hơn mà không làm bài máy móc.

Quy tắc bắt buộc:
- Không viết lại toàn bài nếu nội dung đã ổn.
- Không thêm H1.
- Giữ bố cục HTML hiện có, chỉ bổ sung/sửa đoạn cần thiết.
- Rải focus keyword tự nhiên trong câu có ngữ cảnh, tránh nhồi từ khóa.
- Nếu thiếu outbound/internal link, chèn link do input cung cấp vào câu phù hợp, không dùng rel="nofollow".
- Không nhắc website nguồn, URL nguồn, thương hiệu nguồn hoặc cụm "nguồn tham khảo".
- Không thêm bảng thông số giả, không thêm dữ kiện không có cơ sở.
- Trả về JSON: {"html": "...", "notes": ["..."]}.
""".strip()


def _html_to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _keyword_density(html: str, focus_keyword: str) -> tuple[int, int, float]:
    text = _html_to_text(html).lower()
    keyword = (focus_keyword or "").lower().strip()
    words = len(text.split())
    count = text.count(keyword) if keyword else 0
    return words, count, round((count / max(words, 1)) * 100, 2)


def _external_links(html: str, source_url: str | None) -> list[str]:
    source_netloc = urlparse(source_url or "").netloc.lower().replace("www.", "")
    links = []
    for href in re.findall(r'href="(https?://[^"]+)"', html or "", re.IGNORECASE):
        netloc = urlparse(href).netloc.lower().replace("www.", "")
        if not netloc or netloc == "localhost:8090":
            continue
        if source_netloc and netloc == source_netloc:
            continue
        links.append(href)
    return links


def _site_base(state: dict) -> str:
    return str((state.get("site_profile") or {}).get("url") or "").rstrip("/")


def _internal_links_for_state(html: str, state: dict) -> list[str]:
    base = _site_base(state)
    if not base:
        return []
    return re.findall(rf'href="{re.escape(base)}[^"]*"', html or "", re.IGNORECASE)


def _link_candidates(state: dict) -> dict:
    source_url = state.get("fetch_result", {}).get("metadata", {}).get("url")
    source_netloc = urlparse(source_url or "").netloc.lower().replace("www.", "")
    outbound = []
    for item in state.get("additional_sources") or []:
        url = (item or {}).get("url", "")
        title = (item or {}).get("title") or "thông tin liên quan"
        netloc = urlparse(url).netloc.lower().replace("www.", "")
        if url and netloc and netloc != source_netloc:
            outbound.append({"url": url, "title": title})
    internal = []
    base = _site_base(state)
    if base:
        internal.append({"url": f"{base.rstrip('/')}/?post_type=product", "title": "sản phẩm liên quan"})
    return {"outbound": outbound[:3], "internal": internal[:2]}


def _fallback_payload(html: str, focus_keyword: str) -> dict:
    return {"html": html, "notes": [f"Không chỉnh được HTML cho {focus_keyword} bằng LLM."]}


def run(state: dict) -> dict:
    html = state.get("linked_html") or state.get("humanized", {}).get("html") or state.get("draft", {}).get("html") or ""
    focus_keyword = state.get("plan", {}).get("focus_keyword", "")
    if not html or not focus_keyword:
        return state

    words, count, density = _keyword_density(html, focus_keyword)
    target_min = math.ceil(words * 0.01)
    target_max = math.floor(words * 0.015)
    source_url = state.get("fetch_result", {}).get("metadata", {}).get("url")
    candidates = _link_candidates(state)
    needs = {
        "word_count": words,
        "current_keyword_count": count,
        "current_density_pct": density,
        "target_keyword_count_range": [target_min, max(target_min, target_max)],
        "missing_outbound_link": not _external_links(html, source_url),
        "missing_internal_link": not _internal_links_for_state(html, state),
    }

    data = {}
    adjusted = html
    for attempt in range(2):
        current_words, current_count, current_density = _keyword_density(adjusted, focus_keyword)
        current_needs = dict(needs)
        current_needs.update(
            {
                "attempt": attempt + 1,
                "current_word_count": current_words,
                "current_keyword_count": current_count,
                "current_density_pct": current_density,
                "minimum_keyword_count_required": target_min,
                "additional_keyword_mentions_needed": max(0, target_min - current_count),
            }
        )
        prompt = (
            f"Focus keyword: {focus_keyword}\n"
            f"SEO needs: {json.dumps(current_needs, ensure_ascii=False)}\n"
            f"Link candidates: {json.dumps(candidates, ensure_ascii=False)}\n"
            f"Product/source context: {json.dumps(state.get('extracted', {}), ensure_ascii=False)[:3500]}\n"
            f"Current HTML:\n{adjusted}"
        )
        data = call_json(
            "seo_adjuster",
            SEO_ADJUSTER_SYSTEM_PROMPT,
            prompt,
            fallback=_fallback_payload(adjusted, focus_keyword),
            max_tokens=max(2600, min(5200, len(adjusted) // 3)),
        )
        adjusted = data.get("html") if isinstance(data.get("html"), str) else adjusted
        _, current_count, _ = _keyword_density(adjusted, focus_keyword)
        if current_count >= target_min:
            break
    new_words, new_count, new_density = _keyword_density(adjusted, focus_keyword)
    state["linked_html"] = adjusted
    state.setdefault("seo_adjustments", []).append(
        {
            "focus_keyword": focus_keyword,
            "before": {"word_count": words, "keyword_count": count, "density_pct": density},
            "after": {"word_count": new_words, "keyword_count": new_count, "density_pct": new_density},
            "target_keyword_count_range": [target_min, max(target_min, target_max)],
            "notes": data.get("notes") or [],
        }
    )
    return state
