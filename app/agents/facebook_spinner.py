from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from app.llm import call_json


FACEBOOK_SPINNER_SYSTEM_PROMPT = """
Bạn là strategist nội dung Facebook tiếng Việt.
Nhiệm vụ: tạo nhiều caption lõi để đăng lên nhiều fanpage mà không bị giống nhau máy móc.

Nguyên tắc:
- Không spin kiểu thay từ đồng nghĩa.
- Mỗi caption lõi phải khác về hook, angle, cấu trúc hoặc CTA.
- Giữ đúng dữ kiện trong brief, không bịa giá, cam kết, chính sách, khuyến mãi hoặc số điện thoại nếu brief không có.
- Văn phong tự nhiên, phù hợp Facebook, không giống bài SEO website.
- Nếu có nhiều nhóm page, tạo angle hợp từng nhóm.
- Không nhắc rằng đây là biến thể/spin/AI.
- Trả về JSON hợp lệ, không thêm giải thích.

Schema:
{
  "core_captions": [
    {
      "angle": "góc triển khai ngắn",
      "persona": "nhóm/page phù hợp",
      "caption": "caption lõi",
      "hashtags": ["tag1", "tag2"],
      "cta": "câu CTA"
    }
  ]
}
""".strip()


ANGLE_BANK = [
    "lợi ích thực tế khi sử dụng",
    "checklist chọn mua",
    "độ bền và chất liệu",
    "tình huống khách hay gặp",
    "so sánh trước khi chọn",
    "câu chuyện trải nghiệm ngắn",
    "ưu điểm nổi bật nhất",
    "hỏi đáp/kích thích bình luận",
    "gợi ý dùng hoặc bảo quản",
    "nhắc lại offer theo hướng mềm",
]


def _clean_text(value: object, limit: int = 5000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _word_set(text: str) -> set[str]:
    return {item for item in re.findall(r"[\wÀ-ỹ]+", text.lower()) if len(item) > 2}


def _similarity(left: str, right: str) -> float:
    a = _word_set(left)
    b = _word_set(right)
    if not a or not b:
        return 0.0
    return round(len(a & b) / max(1, len(a | b)), 3)


def recommended_core_count(page_count: int) -> int:
    if page_count <= 5:
        return max(1, page_count)
    if page_count <= 10:
        return 8
    if page_count <= 20:
        return 12
    if page_count <= 40:
        return 18
    if page_count <= 80:
        return 28
    return 34


def _fallback_core_captions(brief: str, groups: list[str], count: int, hashtag_count: int) -> list[dict[str, Any]]:
    base = _clean_text(brief, 1200)
    if not base:
        base = "Chia sẻ nội dung mới đến khách hàng quan tâm."
    labels = groups or ["fanpage"]
    items: list[dict[str, Any]] = []
    for index in range(max(1, count)):
        angle = ANGLE_BANK[index % len(ANGLE_BANK)]
        persona = labels[index % len(labels)]
        caption = (
            f"{angle.capitalize()} là góc đáng chú ý hôm nay.\n\n"
            f"{base}\n\n"
            "Nếu bạn đang quan tâm, hãy nhắn tin để được tư vấn kỹ hơn."
        )
        items.append(
            {
                "angle": angle,
                "persona": persona,
                "caption": caption,
                "hashtags": [f"#{re.sub(r'\\W+', '', word).lower()}" for word in persona.split()[:hashtag_count] if word][:hashtag_count],
                "cta": "Nhắn tin để được tư vấn kỹ hơn.",
            }
        )
    return items


def _normalize_core_items(items: object, fallback: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return fallback
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        caption = _clean_text(item.get("caption"), 2600)
        if not caption:
            continue
        hashtags = item.get("hashtags") if isinstance(item.get("hashtags"), list) else []
        normalized.append(
            {
                "angle": _clean_text(item.get("angle"), 120) or ANGLE_BANK[len(normalized) % len(ANGLE_BANK)],
                "persona": _clean_text(item.get("persona"), 120) or "fanpage",
                "caption": caption,
                "hashtags": [_clean_text(tag, 40) for tag in hashtags if _clean_text(tag, 40)][:8],
                "cta": _clean_text(item.get("cta"), 240),
            }
        )
        if len(normalized) >= count:
            break
    return normalized or fallback


def _personalize_caption(core: dict[str, Any], page: dict[str, Any], index: int, hashtag_count: int) -> dict[str, Any]:
    page_name = _clean_text(page.get("name"), 120) or _clean_text(page.get("page_id"), 40) or "Fanpage"
    group = _clean_text(page.get("group"), 120) or "Chưa có nhóm"
    caption = _clean_text(core.get("caption"), 3000)
    cta = _clean_text(core.get("cta"), 240)
    if cta and cta.lower() not in caption.lower():
        caption = f"{caption.rstrip()}\n\n{cta}"
    suffix_options = [
        f"Bạn ở page {page_name} muốn xem thêm góc nào?",
        "Bạn muốn mình lên thêm bài so sánh hay checklist chi tiết?",
        "Nếu cần tư vấn nhanh, cứ nhắn trực tiếp cho page.",
        "Bạn thấy điểm nào đáng quan tâm nhất?",
    ]
    suffix = suffix_options[index % len(suffix_options)]
    if suffix.lower() not in caption.lower():
        caption = f"{caption.rstrip()}\n\n{suffix}"

    hashtags = []
    for tag in core.get("hashtags") or []:
        clean = _clean_text(tag, 40)
        if clean:
            hashtags.append(clean if clean.startswith("#") else f"#{clean}")
    for word in [group, page_name]:
        slug = re.sub(r"\W+", "", word.lower())
        if slug:
            hashtags.append(f"#{slug[:32]}")
    deduped = []
    for tag in hashtags:
        if tag not in deduped:
            deduped.append(tag)
    return {
        "page_id": str(page.get("page_id") or ""),
        "page_name": page_name,
        "group": group,
        "angle": _clean_text(core.get("angle"), 120),
        "caption": caption,
        "hashtags": deduped[: max(0, hashtag_count)],
        "core_index": int(core.get("_core_index", 0)),
    }


def _stable_core_index(page: dict[str, Any], core_count: int) -> int:
    key = f"{page.get('group') or ''}:{page.get('page_id') or page.get('name') or ''}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % max(1, core_count)


def run(
    *,
    brief: str,
    pages: list[dict[str, Any]],
    groups: list[str] | None = None,
    tone: str = "",
    image_count: int = 0,
    hashtag_count: int = 5,
    core_count: int | None = None,
) -> dict[str, Any]:
    page_count = len(pages)
    target_core_count = max(1, min(core_count or recommended_core_count(page_count), max(1, page_count), 40))
    group_names = sorted({str(page.get("group") or "").strip() for page in pages if str(page.get("group") or "").strip()})
    if groups:
        group_names = sorted({*group_names, *[str(group).strip() for group in groups if str(group).strip()]})
    fallback_cores = _fallback_core_captions(brief, group_names, target_core_count, hashtag_count)
    prompt = (
        f"Brief: {_clean_text(brief, 5000)}\n"
        f"Tone ưu tiên: {_clean_text(tone, 160) or 'tự nhiên, bán hàng vừa phải'}\n"
        f"Số page đích: {page_count}\n"
        f"Nhóm page: {group_names}\n"
        f"Số ảnh đính kèm: {image_count}\n"
        f"Số caption lõi cần tạo: {target_core_count}\n"
        f"Số hashtag tối đa mỗi caption: {hashtag_count}\n"
        f"Page sample: {json.dumps([{k: page.get(k) for k in ['page_id', 'name', 'group', 'category']} for page in pages[:30]], ensure_ascii=False)}\n"
        "Yêu cầu: tạo caption lõi đủ khác nhau để map ra từng page. Mỗi caption nên có hook khác, angle khác hoặc cấu trúc khác."
    )
    data = call_json(
        "facebook_spinner",
        FACEBOOK_SPINNER_SYSTEM_PROMPT,
        prompt,
        fallback={"core_captions": fallback_cores},
        max_tokens=min(5200, 900 + target_core_count * 220),
    )
    core_captions = _normalize_core_items(data.get("core_captions"), fallback_cores, target_core_count)
    for index, item in enumerate(core_captions):
        item["_core_index"] = index

    posts: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        core = core_captions[_stable_core_index(page, len(core_captions))]
        posts.append(_personalize_caption(core, page, index, hashtag_count))

    max_similarity = 0.0
    comparisons = 0
    for i in range(len(posts)):
        for j in range(i + 1, min(len(posts), i + 8)):
            max_similarity = max(max_similarity, _similarity(posts[i]["caption"], posts[j]["caption"]))
            comparisons += 1

    return {
        "strategy": "core-caption-plus-page-personalization",
        "page_count": page_count,
        "core_caption_count": len(core_captions),
        "recommended_core_caption_count": recommended_core_count(page_count),
        "core_captions": [{k: v for k, v in item.items() if not k.startswith("_")} for item in core_captions],
        "posts": posts,
        "quality": {
            "max_nearby_similarity": round(max_similarity, 3),
            "comparisons": comparisons,
            "estimated_llm_calls": max(1, math.ceil(len(core_captions) / max(1, target_core_count))),
        },
        "warnings": [],
    }
