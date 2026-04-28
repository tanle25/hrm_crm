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
- Mỗi caption phải có headline/tiêu đề ngắn, thu hút khách trong 1 dòng đầu.
- Body phải dễ đọc: chia đoạn rõ, ưu tiên bullet bằng ký tự • khi liệt kê lợi ích/thông số.
- CTA là bắt buộc, rõ hành động: inbox, bình luận, nhắn page, đặt hàng, hỏi tư vấn; không chung chung.
- CTA nên định dạng thành block cuối 2-3 dòng có emoji phù hợp, ví dụ: 📩 Inbox/Zalo..., 🚚 Giao hàng toàn quốc, ✅ Nhận hàng kiểm tra rồi thanh toán.
- Không lặp CTA trong body. Nếu caption đã có CTA thì trường cta chỉ tóm lại CTA đó, không viết thêm câu khác.
- Nếu có nhiều nhóm page, tạo angle hợp từng nhóm.
- Không nhắc rằng đây là biến thể/spin/AI.
- Trả về JSON hợp lệ, không thêm giải thích.

Schema:
{
  "core_captions": [
    {
      "angle": "góc triển khai ngắn",
      "persona": "nhóm/page phù hợp",
      "headline": "tiêu đề/hook 1 dòng",
      "caption": "caption lõi",
      "hashtags": ["tag1", "tag2"],
      "cta": "câu CTA"
    }
  ]
}
""".strip()


FACEBOOK_REVIEW_SYSTEM_PROMPT = """
Bạn là QA editor cho nội dung Facebook tiếng Việt.
Nhiệm vụ: review từng caption bán hàng và chỉ rewrite caption chưa đạt.

Tiêu chí đạt:
- Dòng đầu là headline/hook rõ, thu hút, không quá dài.
- Body dễ đọc, chia đoạn hoặc bullet rõ ràng.
- CTA nằm cuối, không bị lặp, có hành động cụ thể.
- Nếu brief có giao hàng/COD/nhận hàng thanh toán thì CTA nên thể hiện tự nhiên bằng emoji phù hợp.
- Không bịa giá, cam kết, khuyến mãi, số điện thoại hoặc chính sách ngoài brief.
- Không có câu hỏi/suffix vô nghĩa sau CTA.
- Không nhắc AI, spin, biến thể.

Trả về JSON hợp lệ:
{
  "reviews": [
    {
      "index": 0,
      "pass": true,
      "score": 8.5,
      "issues": [],
      "headline": "",
      "caption": "",
      "cta": "",
      "hashtags": []
    }
  ]
}

Nếu pass=true: có thể để headline/caption/cta/hashtags rỗng.
Nếu pass=false: phải viết lại headline, caption body và cta cho bài đó.
Không thêm giải thích ngoài JSON.
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


def _clean_multiline(value: object, limit: int = 5000) -> str:
    lines = []
    for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = re.sub(r"[ \t]+", " ", line).strip()
        if cleaned or (lines and lines[-1]):
            lines.append(cleaned)
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit]


def _word_set(text: str) -> set[str]:
    return {item for item in re.findall(r"[\wÀ-ỹ]+", text.lower()) if len(item) > 2}


def _similarity(left: str, right: str) -> float:
    a = _word_set(left)
    b = _word_set(right)
    if not a or not b:
        return 0.0
    return round(len(a & b) / max(1, len(a | b)), 3)


def _compact_for_compare(value: str) -> str:
    return re.sub(r"[^0-9a-zA-ZÀ-ỹ]+", "", value.lower())


def _contains_similar_text(container: str, needle: str) -> bool:
    compact_container = _compact_for_compare(container)
    compact_needle = _compact_for_compare(needle)
    if not compact_container or not compact_needle:
        return False
    if compact_needle in compact_container:
        return True
    container_words = _word_set(container)
    needle_words = _word_set(needle)
    if len(needle_words) < 4:
        return False
    return len(container_words & needle_words) / max(1, len(needle_words)) >= 0.78


def _default_cta(brief: str) -> str:
    lower = brief.lower()
    lines = ["📩 Inbox page để được tư vấn và nhận hình thực tế."]
    if any(term in lower for term in ["zalo", "hotline", "số điện thoại", "083", "084", "085", "086", "087", "088", "089", "09"]):
        lines[0] = "📩 Inbox page hoặc Zalo/Hotline trong bài để được tư vấn nhanh."
    if any(term in lower for term in ["giao hàng", "ship", "toàn quốc", "cod", "nhận hàng", "thanh toán"]):
        lines.append("🚚 Giao hàng toàn quốc.")
        lines.append("✅ Nhận hàng kiểm tra rồi thanh toán.")
    return "\n".join(lines)


def _body_has_cta(body: str) -> bool:
    lower = body.lower()
    return any(
        term in lower
        for term in [
            "inbox",
            "zalo",
            "hotline",
            "đặt hàng",
            "dat hang",
            "liên hệ",
            "lien he",
            "nhắn",
            "nhan",
            "comment",
            "bình luận",
        ]
    )


def _normalize_cta(cta: str, body: str, brief: str) -> str:
    cta = _clean_multiline(cta, 320)
    if not cta:
        if _body_has_cta(body):
            return ""
        cta = _default_cta(brief)
    if _contains_similar_text(body, cta):
        return ""
    body_lines = [line.strip() for line in body.splitlines() if line.strip()]
    cta_lines = [line.strip() for line in cta.splitlines() if line.strip()]
    if cta_lines and body_lines:
        last_body = body_lines[-1]
        first_cta = cta_lines[0]
        if _contains_similar_text(last_body, first_cta) or _contains_similar_text(first_cta, last_body):
            cta_lines = cta_lines[1:]
    return "\n".join(cta_lines).strip()


def _compose_caption(headline: str, body: str, cta: str) -> str:
    return "\n\n".join(part.strip() for part in [headline, body, cta] if part and part.strip())


def _normalize_hashtags(tags: object, fallback: list[str], limit: int) -> list[str]:
    source = tags if isinstance(tags, list) else fallback
    normalized = []
    for tag in source or []:
        clean = _clean_text(tag, 40)
        if not clean:
            continue
        clean = clean if clean.startswith("#") else f"#{clean}"
        if clean not in normalized:
            normalized.append(clean)
    return normalized[: max(0, limit)]


def _strip_repeated_headline(body: str, headline: str) -> str:
    lines = body.splitlines()
    if not lines:
        return body
    first = lines[0].strip()
    if headline and (_contains_similar_text(first, headline) or _contains_similar_text(headline, first)):
        return "\n".join(lines[1:]).strip()
    return body


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
        headline = f"{angle.capitalize()} - lựa chọn đáng cân nhắc"
        caption = f"{base}\n\n• Dễ xem thông tin chính\n• Phù hợp khách đang cần tư vấn nhanh\n• Có thể hỏi thêm chi tiết trước khi quyết định"
        items.append(
            {
                "angle": angle,
                "persona": persona,
                "headline": headline,
                "caption": caption,
                "hashtags": [f"#{re.sub(r'\\W+', '', word).lower()}" for word in persona.split()[:hashtag_count] if word][:hashtag_count],
                "cta": _default_cta(brief),
            }
        )
    return items


def _normalize_core_items(items: object, fallback: list[dict[str, Any]], count: int, brief: str = "") -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return fallback
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        caption = _clean_multiline(item.get("caption"), 2600)
        if not caption:
            continue
        hashtags = item.get("hashtags") if isinstance(item.get("hashtags"), list) else []
        normalized.append(
            {
                "angle": _clean_text(item.get("angle"), 120) or ANGLE_BANK[len(normalized) % len(ANGLE_BANK)],
                "persona": _clean_text(item.get("persona"), 120) or "fanpage",
                "headline": _clean_text(item.get("headline") or item.get("hook") or item.get("title"), 180),
                "caption": caption,
                "hashtags": [_clean_text(tag, 40) for tag in hashtags if _clean_text(tag, 40)][:8],
                "cta": _normalize_cta(str(item.get("cta") or ""), caption, brief),
            }
        )
        if len(normalized) >= count:
            break
    return normalized or fallback


def _personalize_caption(core: dict[str, Any], page: dict[str, Any], index: int, hashtag_count: int, brief: str) -> dict[str, Any]:
    page_name = _clean_text(page.get("name"), 120) or _clean_text(page.get("page_id"), 40) or "Fanpage"
    group = _clean_text(page.get("group"), 120) or "Chưa có nhóm"
    headline = _clean_text(core.get("headline"), 180)
    if not headline:
        headline = f"{_clean_text(core.get('angle'), 80).capitalize()} cho khách đang quan tâm"
    body = _clean_multiline(core.get("caption"), 3000)
    cta = _normalize_cta(str(core.get("cta") or ""), body, brief)
    caption = _compose_caption(headline, body, cta)
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
        "headline": headline,
        "cta": cta,
        "caption": caption,
        "hashtags": deduped[: max(0, hashtag_count)],
        "core_index": int(core.get("_core_index", 0)),
    }


def _review_batch_prompt(brief: str, tone: str, posts: list[dict[str, Any]]) -> str:
    compact_posts = []
    for index, post in enumerate(posts):
        compact_posts.append(
            {
                "index": index,
                "page_name": post.get("page_name"),
                "group": post.get("group"),
                "headline": post.get("headline"),
                "caption": _clean_multiline(post.get("caption"), 2200),
                "cta": post.get("cta"),
                "hashtags": post.get("hashtags") or [],
            }
        )
    return (
        f"Brief gốc: {_clean_text(brief, 5000)}\n"
        f"Tone ưu tiên: {_clean_text(tone, 180) or 'tự nhiên, bán hàng vừa phải'}\n"
        f"Posts cần review: {json.dumps(compact_posts, ensure_ascii=False)}\n"
        "Hãy review từng post theo index. Nếu post đã đạt, pass=true và không rewrite. "
        "Nếu chưa đạt, pass=false và trả headline/caption/cta/hashtags đã sửa. "
        "CTA rewrite nên là block cuối 2-3 dòng, dùng emoji vừa phải như 📩 🚚 ✅ khi phù hợp với brief. "
        "Không thêm câu kiểu 'Bạn muốn mình...' hoặc câu hỏi sau CTA."
    )


def _apply_review_rewrites(
    posts: list[dict[str, Any]],
    reviews: object,
    *,
    brief: str,
    hashtag_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(reviews, list):
        return posts, {"reviewed": 0, "rewritten": 0, "failed_indexes": []}
    updated = [dict(post) for post in posts]
    rewritten = 0
    failed_indexes: list[int] = []
    for item in reviews:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(updated):
            continue
        if bool(item.get("pass")):
            continue
        post = dict(updated[index])
        headline = _clean_text(item.get("headline"), 180) or str(post.get("headline") or "")
        body = _clean_multiline(item.get("caption"), 3000) or str(post.get("caption") or "")
        body = _strip_repeated_headline(body, headline)
        cta = _normalize_cta(str(item.get("cta") or ""), body, brief)
        post["headline"] = headline
        post["cta"] = cta
        post["caption"] = _compose_caption(headline, body, cta)
        post["hashtags"] = _normalize_hashtags(item.get("hashtags"), post.get("hashtags") or [], hashtag_count)
        post["review"] = {
            "pass": False,
            "score": item.get("score"),
            "issues": item.get("issues") if isinstance(item.get("issues"), list) else [],
            "rewritten": True,
        }
        updated[index] = post
        rewritten += 1
        failed_indexes.append(index)
    return updated, {"reviewed": len(reviews), "rewritten": rewritten, "failed_indexes": failed_indexes}


def _review_and_rewrite_posts(
    posts: list[dict[str, Any]],
    *,
    brief: str,
    tone: str,
    hashtag_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not posts:
        return posts, {"enabled": True, "reviewed": 0, "rewritten": 0, "batches": 0, "failed_indexes": []}
    batch_size = 8
    rewritten_posts = [dict(post) for post in posts]
    summary = {"enabled": True, "reviewed": 0, "rewritten": 0, "batches": 0, "failed_indexes": []}
    for start in range(0, len(rewritten_posts), batch_size):
        batch = rewritten_posts[start : start + batch_size]
        fallback = {"reviews": [{"index": index, "pass": True, "score": 8.0, "issues": []} for index in range(len(batch))]}
        data = call_json(
            "facebook_spinner",
            FACEBOOK_REVIEW_SYSTEM_PROMPT,
            _review_batch_prompt(brief, tone, batch),
            fallback=fallback,
            max_tokens=3200,
        )
        reviewed_batch, batch_summary = _apply_review_rewrites(
            batch,
            data.get("reviews"),
            brief=brief,
            hashtag_count=hashtag_count,
        )
        rewritten_posts[start : start + batch_size] = reviewed_batch
        summary["reviewed"] += int(batch_summary["reviewed"])
        summary["rewritten"] += int(batch_summary["rewritten"])
        summary["batches"] += 1
        summary["failed_indexes"].extend([start + index for index in batch_summary["failed_indexes"]])
    return rewritten_posts, summary


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
        "Tone/format nên luân phiên để nội dung phong phú nhưng vẫn bán hàng: tư vấn trực tiếp, checklist nhanh, kể trải nghiệm, so sánh lựa chọn, "
        "nhấn mạnh lợi ích thực tế, xử lý băn khoăn khách hàng, gợi ý đặt hàng nhẹ nhàng.\n"
        "Không thêm câu hỏi/suffix sau CTA. CTA phải là block cuối cùng của caption.\n"
        "CTA nên dùng emoji vừa phải và định dạng rõ. Nếu brief có thông tin giao hàng/COD/nhận hàng thanh toán thì đưa vào CTA; nếu brief không có thì không bịa cam kết.\n"
        "Không lặp lại cùng một CTA trong body và trường cta.\n"
        "Yêu cầu: tạo caption lõi đủ khác nhau để map ra từng page. "
        "Mỗi caption phải có headline thu hút, body chia đoạn/bullet rõ ràng và CTA cụ thể ở cuối."
    )
    data = call_json(
        "facebook_spinner",
        FACEBOOK_SPINNER_SYSTEM_PROMPT,
        prompt,
        fallback={"core_captions": fallback_cores},
        max_tokens=min(5200, 900 + target_core_count * 220),
    )
    core_captions = _normalize_core_items(data.get("core_captions"), fallback_cores, target_core_count, brief)
    for index, item in enumerate(core_captions):
        item["_core_index"] = index

    posts: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        core = core_captions[_stable_core_index(page, len(core_captions))]
        posts.append(_personalize_caption(core, page, index, hashtag_count, brief))
    posts, review_summary = _review_and_rewrite_posts(posts, brief=brief, tone=tone, hashtag_count=hashtag_count)

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
            "estimated_llm_calls": max(1, math.ceil(len(core_captions) / max(1, target_core_count))) + int(review_summary.get("batches") or 0),
            "review": review_summary,
        },
        "warnings": [],
    }
