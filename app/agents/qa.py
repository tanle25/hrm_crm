from __future__ import annotations

import re
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import urlparse

from app.config import get_settings
from app.llm import call_json


QA_SYSTEM_PROMPT = """
Bạn là QA editor cho nội dung SEO/GEO tiếng Việt.
Nếu là product content, phải kiểm tra nghiêm:
- tiêu đề là tên sản phẩm tự nhiên, không biến thành tiêu đề blog
- không ghi website nguồn, URL nguồn, thương hiệu nguồn hoặc cụm "nguồn tham khảo"
- có bảng, checklist, FAQ, ảnh hợp lệ và alt text chứa focus keyword
- nội dung có thông tin cụ thể từ dữ liệu đã extract, không chỉ nói chung chung
- văn phong tiếng Việt tự nhiên, không máy móc, không lặp cụm sáo rỗng

Trả về JSON hợp lệ theo schema:
scores {eeat_score, geo_structure_score, readability_score, rank_math_readiness},
overall_score, pass, feedback {strengths, improvements, retry_target}.
Không thêm giải thích.
""".strip()


def _site_base_url(state: dict) -> str:
    return str((state.get("site_profile") or {}).get("url") or "").rstrip("/")


def _source_origin(state: dict) -> str:
    return str(state.get("source_origin") or "").strip().lower()


def check_plagiarism(source: str, generated: str) -> float:
    source_clean = " ".join(source.lower().split())
    generated_clean = " ".join(generated.lower().split())
    if not source_clean or not generated_clean:
        return 0.0
    matcher = SequenceMatcher(None, source_clean, generated_clean, autojunk=False)
    return round(matcher.ratio(), 3)


def _html_to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _source_terms(state: dict) -> list[str]:
    metadata = state["fetch_result"].get("metadata", {})
    url = metadata.get("url", "")
    netloc = urlparse(url).netloc.lower().replace("www.", "")
    terms = []
    if netloc:
        terms.extend([netloc, netloc.split(".")[0]])
    sitename = (metadata.get("sitename") or "").lower().strip()
    if sitename:
        terms.append(sitename.replace("www.", ""))
        terms.append(sitename.split(".")[0])
    return [term for term in set(terms) if len(term) >= 4]


def _strict_blocks(state: dict, similarity: float) -> list[str]:
    html = state.get("linked_html", "")
    html_lower = html.lower()
    text = _html_to_text(html)
    text_lower = text.lower()
    plan = state["plan"]
    metadata = state["fetch_result"].get("metadata", {})
    source_type = (metadata.get("source_type") or "").lower()
    source_origin = _source_origin(state)
    image_data = state.get("image_data") or {}
    gallery = image_data.get("gallery") or []
    source_gallery = metadata.get("image_urls") or []
    image_ready = bool(gallery or source_gallery or get_settings().unsplash_access_key)
    focus_keyword = plan["focus_keyword"].lower()
    blocks: list[str] = []

    if similarity >= 0.35:
        blocks.append(f"Similarity quá cao ({similarity:.1%}); cần viết lại mạnh hơn.")
    if source_type == "product":
        forbidden_terms = ["nguồn tham khảo", "website nguồn", "url nguồn", "traviet", "trà việt"]
        forbidden_terms.extend(_source_terms(state))
        for term in forbidden_terms:
            if term and term in text_lower:
                blocks.append(f"Nội dung product còn nhắc nguồn/thương hiệu nguồn: {term}.")
                break
        title_lower = plan["title"].lower()
        if any(term in title_lower for term in ["hướng dẫn", "huong dan", "phân tích", "phan tich", "tổng hợp", "tong hop"]):
            blocks.append("Title product vẫn giống tiêu đề blog, chưa phải tên sản phẩm tự nhiên.")
        if not image_ready:
            blocks.append("Product chưa có nguồn ảnh hợp lệ cho gallery/featured image.")
        if gallery and focus_keyword not in (image_data.get("alt_text") or "").lower():
            blocks.append("Alt text ảnh chưa chứa focus keyword.")
        opening_lines = [line.strip() for line in text.split(".") if line.strip()][:4]
        if any("gợi ý nhanh" in line.lower() or "mô tả ngắn" in line.lower() for line in opening_lines):
            blocks.append("Mở bài còn dùng nhãn 'Gợi ý nhanh' hoặc heading 'Mô tả ngắn', chưa đúng yêu cầu hiện tại.")
        if "<img " not in html_lower:
            blocks.append("Thiếu hình ảnh trong thân bài.")
        keyword_density = (text_lower.count(focus_keyword) / max(len(text.split()), 1)) * 100
        if keyword_density < 0.8:
            blocks.append(f"Mật độ focus keyword {keyword_density:.2f}% còn thấp; cần rải tự nhiên hơn, tối thiểu 0.8% và tốt nhất khoảng 1-1.5%.")
        source_netloc = urlparse(metadata.get("url", "")).netloc.lower().replace("www.", "")
        woo_netloc = urlparse(_site_base_url(state)).netloc.lower().replace("www.", "")
        external_links = []
        for href in re.findall(r'href="(https?://[^"]+)"', html, re.IGNORECASE):
            netloc = urlparse(href).netloc.lower().replace("www.", "")
            if not netloc:
                continue
            if source_netloc and netloc == source_netloc:
                continue
            if woo_netloc and netloc == woo_netloc:
                continue
            if netloc == "localhost:8090":
                continue
            external_links.append(href)
        if not external_links:
            blocks.append("Thiếu outbound link dofollow hợp lệ; cần thêm liên kết ngoài phù hợp ngữ cảnh.")
        woo_base = _site_base_url(state)
        if woo_base and f'href="{woo_base}' not in html and source_origin != "shopee":
            blocks.append("Thiếu internal link phù hợp trong nội dung.")
        extracted = state.get("extracted", {})
        components = extracted.get("product_components") or []
        component_count = len(components)
        claimed_count_match = re.search(r"(?:gồm|bao gồm)\s+(\d+)\s+(?:thành phần|món|chi tiết|loại)", text_lower)
        if claimed_count_match and component_count and int(claimed_count_match.group(1)) != component_count:
            blocks.append("Nội dung đang mâu thuẫn giữa số lượng thành phần nêu trong bài và dữ liệu đã extract.")
        specs = extracted.get("product_specs") or {}
        if (specs.get("packets_per_box") or specs.get("grams_per_packet")) and any(
            marker in text_lower for marker in ["chưa xác nhận từ dữ liệu nguồn", "chưa thấy nêu rõ trong dữ liệu nguồn"]
        ):
            blocks.append("Bảng tóm tắt đang bỏ sót thông số vốn đã có trong extractor.")
        has_specifics = bool(extracted.get("product_components") or extracted.get("product_use_cases") or extracted.get("important_facts"))
        if not has_specifics:
            blocks.append("Extractor chưa cung cấp dữ kiện cụ thể cho product.")
        generic_phrases = [
            "nội dung được biên soạn lại",
            "nguồn gốc",
            "thông tin tổng hợp",
            "nội dung dưới đây",
            "là lựa chọn an toàn",
        ]
        if sum(1 for phrase in generic_phrases if phrase in text_lower) >= 2:
            blocks.append("Văn phong còn nhiều cụm công thức, cần viết tự nhiên hơn.")
        repeated_sentences = re.findall(r"([^.?!]{40,}[.?!])", text)
        normalized = [re.sub(r"\s+", " ", sentence).strip().lower() for sentence in repeated_sentences]
        if len(normalized) != len(set(normalized)):
            blocks.append("Nội dung đang lặp lại ý hoặc lặp nguyên câu.")
    return blocks


def _score(max_score: int, checks: list[bool]) -> int:
    if not checks:
        return 0
    return min(max_score, round(max_score * sum(1 for check in checks if check) / len(checks)))


def _weighted_overall(scores: dict) -> float:
    return round(
        float(scores.get("eeat_score", 0)) * 0.25
        + float(scores.get("geo_structure_score", 0)) * 0.30
        + float(scores.get("readability_score", 0)) * 0.25
        + float(scores.get("rank_math_readiness", 0)) * 0.20,
        2,
    )


def _passes_rubric(scores: dict, similarity: float) -> bool:
    return (
        similarity < 0.35
        and float(scores.get("eeat_score", 0)) >= 6
        and float(scores.get("geo_structure_score", 0)) >= 7
        and float(scores.get("readability_score", 0)) >= 6
        and float(scores.get("rank_math_readiness", 0)) >= 7
        and _weighted_overall(scores) >= 7.0
    )


def _normalize_retry_target(raw_target: object, fallback_target: str, issue_category: str) -> str:
    target = str(raw_target or "").strip().lower()
    allowed = {"writer", "humanizer", "seo_adjuster"}
    if target in allowed:
        return target
    if issue_category == "seo_minor":
        return "seo_adjuster"
    if issue_category == "style_editing":
        return "humanizer"
    return fallback_target


def _is_seo_minor_block(block: str) -> bool:
    block_lower = str(block or "").lower()
    return any(
        marker in block_lower
        for marker in [
            "mật độ focus keyword",
            "outbound link",
            "internal link",
        ]
    )


def _heuristic_qa(state: dict, similarity: float) -> dict:
    html = state.get("linked_html", "")
    html_lower = html.lower()
    text = _html_to_text(html)
    text_lower = text.lower()
    plan = state["plan"]
    source_type = (state["fetch_result"]["metadata"].get("source_type") or "").lower()
    source_origin = _source_origin(state)
    image_data = state.get("image_data") or {}
    gallery = image_data.get("gallery") or []
    source_gallery = (state["fetch_result"]["metadata"].get("image_urls") or [])
    image_ready = bool(gallery or source_gallery or get_settings().unsplash_access_key)
    focus_keyword = plan["focus_keyword"].lower()
    word_count = len(text.split())

    eeat_score = _score(10, [
        bool(state.get("extracted", {}).get("important_facts")),
        bool(state.get("knowledge_facts")),
        bool(state.get("extracted", {}).get("product_use_cases") or source_type != "product"),
        similarity < 0.25,
    ])
    geo_checks = [
        "<ul>" in html_lower,
        "<img " in html_lower if source_type == "product" else True,
        "<h2" in html_lower,
    ]
    if source_origin != "shopee":
        geo_checks.extend([
            ("faq" in html_lower) or ("câu hỏi thường gặp" in html_lower),
            "<table>" in html_lower,
        ])
    else:
        geo_checks.extend([
            ("faq" in html_lower) or ("câu hỏi thường gặp" in html_lower) or word_count >= 1400,
            "<table>" in html_lower or word_count >= 1400,
        ])
    geo_score = _score(10, geo_checks)
    readability_score = _score(10, [
        word_count >= (650 if source_type == "product" else 500),
        "nội dung dưới đây" not in text_lower,
        "nguồn gốc" not in text_lower if source_type == "product" else True,
        len(re.findall(r"[.!?]", text)) >= 18,
    ])
    rank_checks = [
        focus_keyword in plan["meta_title"].lower() or focus_keyword in plan["title"].lower(),
        focus_keyword in plan["meta_description"].lower(),
        focus_keyword in text_lower,
        len(re.findall(r'<a href="https?://[^"]+"', html, re.IGNORECASE)) >= 1 or source_origin == "shopee",
        image_ready,
    ]
    rank_math = _score(10, rank_checks)

    blocks = _strict_blocks(state, similarity)
    score_map = {
        "eeat_score": eeat_score,
        "geo_structure_score": geo_score,
        "readability_score": readability_score,
        "rank_math_readiness": rank_math,
    }
    overall = _weighted_overall(score_map)
    if source_origin == "shopee":
        severe_blocks = [block for block in blocks if not _is_seo_minor_block(block)]
        passed = _passes_rubric(score_map, similarity) and not severe_blocks
    else:
        passed = _passes_rubric(score_map, similarity) and not blocks
    improvements = blocks[:]
    if readability_score < 8:
        improvements.append("Cần tăng độ tự nhiên, giảm câu chung chung và thêm quan sát mua hàng thực tế.")
    if rank_math < 8:
        improvements.append("Cần tối ưu focus keyword trong meta, nội dung, alt text và link.")
    if eeat_score < 7:
        improvements.append("Cần thêm dữ kiện cụ thể từ extractor/knowledge để bài có chiều sâu.")
    if source_type == "product":
        if source_origin != "shopee" and "<table>" not in html_lower:
            improvements.append("Nên bổ sung bảng tóm tắt để người đọc quét thông tin nhanh hơn.")
        if source_origin != "shopee" and "<ul>" not in html_lower:
            improvements.append("Nên có thêm checklist hoặc bullet list để tăng khả năng quét nội dung.")
        faq_heading = ("faq" in html_lower) or ("câu hỏi thường gặp" in html_lower)
        if source_origin != "shopee" and not faq_heading:
            improvements.append("Nên có phần FAQ rõ ràng để tăng khả năng trả lời ý định mua hàng.")
        external_links = [
            href for href in re.findall(r'href="(https?://[^"]+)"', html, re.IGNORECASE)
            if urlparse(href).netloc.lower().replace("www.", "")
            not in {
                "",
                "localhost:8090",
                urlparse(_site_base_url(state)).netloc.lower().replace("www.", ""),
                urlparse(state["fetch_result"]["metadata"].get("url", "")).netloc.lower().replace("www.", ""),
            }
        ]
        if not external_links and source_origin != "shopee":
            improvements.append("Nên có ít nhất một outbound link hợp lệ để hỗ trợ tín hiệu SEO.")
        density = (text_lower.count(focus_keyword) / max(len(text.split()), 1)) * 100
        if density < 0.8:
            improvements.append("Mật độ focus keyword còn thấp; nên rải tự nhiên hơn, tối thiểu 0.8% và tốt nhất khoảng 1-1.5%.")
        elif density > 1.7:
            improvements.append("Mật độ focus keyword hơi cao; cần giảm nhồi từ khóa để giữ văn phong tự nhiên.")
    if not improvements:
        improvements.append("Nội dung đạt ngưỡng QA hiện tại.")

    issue_category = "content_quality"
    if source_type == "product":
        density = (text_lower.count(focus_keyword) / max(len(text.split()), 1)) * 100
        seo_only_blocks = blocks and all(
            any(marker in block for marker in ["Mật độ focus keyword", "outbound link", "internal link"])
            for block in blocks
        )
        if seo_only_blocks:
            issue_category = "seo_minor"
        elif blocks and all("ảnh" in block.lower() or "image" in block.lower() for block in blocks):
            issue_category = "media"
        elif not blocks and (density < 0.8 or rank_math < 8):
            issue_category = "seo_minor"
        elif readability_score < 8:
            issue_category = "style_editing"
        if source_origin == "shopee" and issue_category == "content_quality":
            issue_category = "seo_minor"

    return {
        "scores": {"plagiarism_similarity": similarity, **score_map},
        "overall_score": overall,
        "pass": passed,
        "feedback": {
            "strengths": ["Có cấu trúc chính nếu đủ bảng, checklist, FAQ và ảnh"],
            "improvements": improvements,
            "retry_target": "seo_adjuster" if issue_category == "seo_minor" else ("writer" if blocks or readability_score < 8 else "humanizer"),
            "issue_category": issue_category,
        },
        "retry_count": state.get("qa_result", {}).get("retry_count", 0) + 1,
    }


def run(state: dict) -> dict:
    source = state["fetch_result"]["clean_content"]
    generated = state["linked_html"]
    similarity = check_plagiarism(source, generated)
    fallback = _heuristic_qa(state, similarity)

    prompt = (
        f"Similarity: {similarity}\n"
        f"Strict heuristic result: {fallback}\n"
        f"Title: {state['plan']['title']}\n"
        f"Focus keyword: {state['plan']['focus_keyword']}\n"
        f"Meta description: {state['plan']['meta_description']}\n"
        f"HTML:\n{state['linked_html'][:2600]}"
    )
    data = call_json("qa", QA_SYSTEM_PROMPT, prompt, fallback=fallback, max_tokens=1100)
    data.setdefault("scores", {})
    data["scores"]["plagiarism_similarity"] = similarity
    for score_key, score_value in fallback["scores"].items():
        if score_key == "plagiarism_similarity":
            continue
        data["scores"][score_key] = float(score_value)
    if "feedback" not in data:
        data["feedback"] = fallback["feedback"]
    if "retry_target" not in data["feedback"]:
        data["feedback"]["retry_target"] = fallback["feedback"]["retry_target"]
    if "issue_category" not in data["feedback"]:
        data["feedback"]["issue_category"] = fallback["feedback"].get("issue_category", "content_quality")
    data["feedback"]["retry_target"] = _normalize_retry_target(
        data["feedback"].get("retry_target"),
        fallback["feedback"]["retry_target"],
        data["feedback"].get("issue_category", "content_quality"),
    )
    data["retry_count"] = state.get("qa_result", {}).get("retry_count", 0) + 1

    blocks = _strict_blocks(state, similarity)
    scores = data.setdefault("scores", {})
    derived_scores = {
        "eeat_score": float(scores.get("eeat_score", fallback["scores"]["eeat_score"])),
        "geo_structure_score": float(scores.get("geo_structure_score", fallback["scores"]["geo_structure_score"])),
        "readability_score": float(scores.get("readability_score", fallback["scores"]["readability_score"])),
        "rank_math_readiness": float(scores.get("rank_math_readiness", fallback["scores"]["rank_math_readiness"])),
    }
    derived_overall = _weighted_overall(derived_scores)
    severe_blocks = [block for block in blocks if not _is_seo_minor_block(block)]
    seo_minor_blocks = [block for block in blocks if _is_seo_minor_block(block)]
    if _source_origin(state) == "shopee":
        relaxed_markers = [
            "thiếu outbound link dofollow hợp lệ",
            "thiếu internal link phù hợp",
        ]
        severe_blocks = [
            block for block in severe_blocks
            if not any(marker in block.lower() for marker in relaxed_markers)
        ]
    if severe_blocks:
        seo_only_blocks = not severe_blocks and bool(seo_minor_blocks)
        data["pass"] = False
        data["overall_score"] = min(float(data.get("overall_score", derived_overall)), 6.9)
        improvements = data.setdefault("feedback", {}).setdefault("improvements", [])
        for block in blocks:
            if block not in improvements:
                improvements.append(block)
        data["feedback"]["retry_target"] = "seo_adjuster" if seo_only_blocks else "writer"
        media_only_blocks = all("ảnh" in block.lower() or "image" in block.lower() for block in severe_blocks)
        data["feedback"]["issue_category"] = "seo_minor" if seo_only_blocks else ("media" if media_only_blocks else "content_quality")
    else:
        data["overall_score"] = derived_overall
        data["pass"] = _passes_rubric(derived_scores, similarity)
        improvements = data.setdefault("feedback", {}).setdefault("improvements", [])
        for block in seo_minor_blocks:
            if block not in improvements:
                improvements.append(block)
        if seo_minor_blocks and data["feedback"].get("issue_category") in {None, "", "content_quality"}:
            data["feedback"]["issue_category"] = "seo_minor"
        if seo_minor_blocks and data["feedback"].get("retry_target") not in {"seo_adjuster", "writer", "humanizer"}:
            data["feedback"]["retry_target"] = "seo_adjuster"
        if not data["pass"]:
            data.setdefault("feedback", {}).setdefault("improvements", [])
            for item in fallback["feedback"]["improvements"]:
                if item not in data["feedback"]["improvements"]:
                    data["feedback"]["improvements"].append(item)
            data["feedback"]["retry_target"] = fallback["feedback"]["retry_target"]
        else:
            data["feedback"]["strengths"] = data["feedback"].get("strengths") or fallback["feedback"]["strengths"]
    for key, value in fallback.items():
        data.setdefault(key, value)
    return data
