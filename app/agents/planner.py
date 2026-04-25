from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from app.llm import call_json
from app.source_cleaner import clean_source_object, contains_source_term


PLANNER_SYSTEM_PROMPT = """
Ban la Planner 2026 cho noi dung tieng Viet.
Neu nguon la product, hay lap ke hoach cho mo ta san pham thuong mai:
- title là tên sản phẩm tự nhiên, không biến thành tiêu đề blog kiểu "hướng dẫn", "phân tích"
- title có thể biên tập lại tên nguồn cho gọn, rõ ý mua hàng hơn; không bắt buộc giữ nguyên từng chữ nếu tên nguồn dài, nhiễu hoặc chứa ngữ cảnh không chính.
- meta_title là tiêu đề SEO riêng, có thể tối ưu Rank Math nhưng không thay tên sản phẩm
- meta_title không dùng một công thức cố định. Hãy tự chọn dạng phù hợp: tên sản phẩm thuần, tên + quy cách, tên + lợi ích chính, hoặc tên + nhóm người dùng nếu dữ liệu thật sự hỗ trợ.
- meta_title bắt buộc chứa focus_keyword hoặc cụm từ khóa chính tương đương gần như nguyên vẹn, ưu tiên đặt ở đầu câu.
- nếu dữ liệu sản phẩm đủ rõ, meta_title nên cố gắng có:
  focus keyword tự nhiên ở gần đầu,
  1 yếu tố tạo cảm xúc tích cực hoặc tiêu cực nhẹ,
  1 power word phù hợp,
  và 1 con số nếu con số đó đến từ dữ liệu thật như số thành phần, số điểm nổi bật, số đặc tính rõ ràng
- với product, nếu dữ liệu đã có con số rõ như số gói, số thành phần, kích thước hay số điểm mạnh nổi bật, hãy ưu tiên dùng con số đó trong meta_title thay vì viết chung chung
- sentiment word và power word phải nghe tự nhiên, ví dụ theo tinh thần tích cực, nổi bật, đáng cân nhắc, tiện dụng, tinh gọn; không dùng nếu làm câu gượng
- không được nhồi nhét hoặc dùng số/power word khi dữ liệu không đủ tự nhiên
- focus_keyword đủ rõ ý định mua hoặc sử dụng, nhưng không tự kéo sang bối cảnh tặng quà nếu tên sản phẩm không thực sự xoay quanh điều đó
- nếu metadata.source_type là product và product_kind là variable, kế hoạch phải giúp người mua hiểu các biến thể/quy cách và cách chọn
- nếu metadata.source_type là article, không lập kế hoạch như trang bán hàng và không ép schema Product
- meta_description <= 155 ky tu
- outline cần bám đúng sản phẩm đang có, không mặc định kéo mọi sản phẩm về cùng một câu chuyện như "quà biếu", "quà tặng" hay "món quà doanh nghiệp"
- narrative phải cân bằng giữa bản chất sản phẩm, trải nghiệm dùng, chất liệu/thiết kế, lợi ích thực tế và các bối cảnh sử dụng có thật; chỉ nhấn mạnh một use case duy nhất khi dữ liệu nguồn cho thấy điều đó là trung tâm rõ ràng
- heading phải tự nhiên, có nhịp biên tập, tránh kiểu câu nào cũng là lời hứa bán hàng hoặc khẩu hiệu quảng cáo
- FAQ nên là nhóm câu hỏi mà người mua thực sự hay băn khoăn trước khi quyết định, không chỉ tách thông số kỹ thuật thành câu hỏi
- schema_type uu tien Product neu phu hop

Tra ve JSON hop le voi cac truong:
title, meta_title, article_type, target_intent, tone, seo_geo_keywords, tags, focus_keyword,
meta_description, outline, e_e_a_t_elements, schema_type.
Khong them giai thich.
""".strip()


def _heuristic_plan(
    key_points: list[str],
    knowledge_facts: list[dict],
    metadata: dict,
    focus_keyword_override: str | None,
    extracted: dict | None = None,
) -> dict:
    extracted = extracted or {}
    product_hints = metadata.get("product_hints") or {}
    base_title = _clean_title(
        product_hints.get("og_title")
        or metadata.get("title")
        or metadata.get("og_title")
        or metadata.get("sitename")
        or urlparse(metadata.get("url", "")).netloc
    )
    source_type = (metadata.get("source_type") or "").lower()
    product_kind = (metadata.get("product_kind") or "").lower()
    archetype = _infer_product_archetype(base_title, extracted) if source_type == "product" else ""
    if source_type == "product":
        focus_keyword = focus_keyword_override or _short_product_focus_keyword(base_title, extracted)
    else:
        focus_keyword = focus_keyword_override or (key_points[0][:60] if key_points else base_title)
    specs = extracted.get("product_specs") or {}
    packets = specs.get("packets_per_box")
    grams = specs.get("grams_per_packet")
    component_count = specs.get("component_count")
    article_type = "Product Description" if source_type == "product" else ("How-to" if any("buoc" in point.lower() for point in key_points) else "Comprehensive Guide")
    outline = {
        "intro": "TL;DR + tra loi truc tiep cau hoi chinh cua nguoi dung.",
        "sections": [
            (
                {"h2": "Tong quan san pham", "content_hint": "Tom tat ban chat san pham, nguon goc va gia tri noi bat."}
                if archetype == "single_tea"
                else {"h2": "Tong quan san pham", "content_hint": "Tom tat ban chat san pham, boi canh su dung va gia tri noi bat."}
            ) if source_type == "product" else {"h2": f"{focus_keyword} la gi?", "content_hint": "Giai thich ngan gon, boi canh, loi ich."},
            (
                {"h2": "Huong vi va cam nhan", "content_hint": "Lam ro huong, vi, nuoc tra, cam giac khi uong va diem de nhan ra."}
                if archetype == "single_tea"
                else {"h2": "Diem dang chu y", "content_hint": "Tap trung vao chat lieu, cau tao, thanh phan hoac chi tiet dang gia chu y."}
            ) if source_type == "product" else {"h2": f"Khi nao nen quan tam den {focus_keyword}?", "content_hint": "Tinh huong ap dung va luu y."},
            (
                {"h2": "Cach pha va doi tuong phu hop", "content_hint": "Tap trung vao cach dung, nguoi hop vi, boi canh uong va luu y khi chon mua."}
                if archetype == "single_tea"
                else {"h2": "Trai nghiem su dung thuc te", "content_hint": "Lam ro cam giac dung, doi tuong phu hop, tinh huong su dung va dieu nguoi mua can can nhac."}
            ) if source_type == "product" else {"h2": f"So sanh nhanh ve {focus_keyword}?", "content_hint": "comparison table"},
            {"h2": "Cau hoi thuong gap", "content_hint": "Nhom cau hoi mua hang thuc te, khong chi tach thong so thanh cau hoi."},
        ],
        "conclusion": "Tom tat + CTA mem, huong nguoi doc den buoc tiep theo.",
    }
    return {
        "title": base_title if source_type == "product" else f"{base_title}: huong dan tong hop va phan tich thuc te",
        "meta_title": _product_meta_title(focus_keyword)[:60] if source_type == "product" else f"{base_title}: hướng dẫn thực tế"[:60],
        "article_type": article_type,
        "target_intent": "commercial" if source_type == "product" or "san pham" in base_title.lower() else "informational",
        "tone": "professional",
        "seo_geo_keywords": [focus_keyword, base_title.lower(), "thông tin chi tiết", "câu hỏi thường gặp"] if source_type == "product" else [focus_keyword, f"{focus_keyword} viet nam", "hướng dẫn thực tế", "câu hỏi thường gặp"],
        "tags": _fallback_tags(base_title, focus_keyword, key_points, extracted),
        "focus_keyword": focus_keyword,
        "meta_description": (
            (
                f"{focus_keyword} được trình bày rõ về thiết kế, trải nghiệm dùng và những điểm đáng cân nhắc trước khi chọn mua."
                if not (packets and grams)
                else f"{focus_keyword} có quy cách {packets} đơn vị x {grams}g, thông tin rõ ràng và phù hợp nhu cầu sử dụng thực tế."
            )
            if source_type == "product"
            else f"Tóm tắt {focus_keyword} theo nguồn gốc, cấu trúc dễ đọc và tối ưu SEO/GEO cho thị trường Việt Nam."
        )[:155],
        "outline": outline,
        "e_e_a_t_elements": {
            "author_note": True,
            "publish_date": True,
            "source_citations": False if source_type == "product" else True,
            "experience_signals": ["vi du thuc te", "ghi chu van hanh"],
        },
        "schema_type": "Product" if source_type == "product" else ("HowTo" if article_type == "How-to" else "Article"),
        "knowledge_count": len(knowledge_facts),
        "product_kind": product_kind,
    }

def run(key_points: list[str], knowledge_facts: list[dict], metadata: dict, focus_keyword_override: str | None, extracted: dict | None = None) -> dict:
    original_metadata = metadata
    metadata = clean_source_object(metadata, original_metadata)
    key_points = clean_source_object(key_points, original_metadata)
    knowledge_facts = clean_source_object(knowledge_facts, original_metadata)
    extracted = clean_source_object(extracted or {}, original_metadata)
    fallback = _heuristic_plan(key_points, knowledge_facts, metadata, focus_keyword_override, extracted)
    prompt = (
        f"Metadata: {metadata}\n"
        f"Key points: {key_points}\n"
        f"Knowledge facts: {knowledge_facts[:5]}\n"
        f"Extracted: {extracted or {}}\n"
        f"Focus keyword override: {focus_keyword_override or ''}\n"
    )
    data = call_json("planner", PLANNER_SYSTEM_PROMPT, prompt, fallback=fallback, max_tokens=1800)
    source_type = (metadata.get("source_type") or "").lower()
    for key, value in fallback.items():
        data.setdefault(key, value)
    if source_type == "product":
        source_title = _clean_title(metadata.get("title") or fallback["title"])
        title = _refine_product_title(str(data.get("title") or ""), source_title, original_metadata)
        data["title"] = title
        data["article_type"] = "Product Description"
        data["target_intent"] = "commercial"
        data["schema_type"] = "Product"
        data["product_kind"] = metadata.get("product_kind") or fallback.get("product_kind", "")
        data.setdefault("outline", fallback["outline"])
        data.setdefault("seo_geo_keywords", fallback["seo_geo_keywords"])

        title_keyword = _short_product_focus_keyword(title, extracted)
        focus_keyword = _clean_title(str(data.get("focus_keyword") or fallback["focus_keyword"])).lower()
        if contains_source_term(focus_keyword, original_metadata) or any(term in focus_keyword for term in ["http://", "https://"]):
            focus_keyword = fallback["focus_keyword"]
        if len(focus_keyword.split()) < 2:
            focus_keyword = fallback["focus_keyword"]
        if len(focus_keyword.split()) > 8:
            focus_keyword = fallback["focus_keyword"]
        if source_type == "product":
            title_words = set(re.findall(r"[\wà-ỹ]+", title_keyword.lower()))
            focus_words = set(re.findall(r"[\wà-ỹ]+", focus_keyword.lower()))
            overlap = len(title_words & focus_words)
            if title_words and overlap < max(2, len(title_words) - 1):
                focus_keyword = title_keyword
            elif _infer_product_archetype(title, extracted) == "single_tea" and (focus_words - title_words):
                focus_keyword = title_keyword
        data["focus_keyword"] = focus_keyword

        meta_title = _refine_product_meta_title(
            str(data.get("meta_title") or fallback["meta_title"]),
            title=title,
            focus_keyword=data["focus_keyword"],
            extracted=extracted,
        )
        data["meta_title"] = meta_title or fallback["meta_title"]

        meta_description = str(data.get("meta_description") or "").strip()
        if not meta_description or data["focus_keyword"] not in meta_description.lower() or len(meta_description) > 155:
            meta_description = fallback["meta_description"]
        data["meta_description"] = meta_description
        data["e_e_a_t_elements"] = fallback["e_e_a_t_elements"]
    data["tags"] = _normalize_tags(data.get("tags"), fallback["tags"], data.get("focus_keyword", fallback["focus_keyword"]))
    return data


def _clean_title(value: str) -> str:
    title = html.unescape(value or "").strip()
    title = re.sub(r"^p/", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^www\.", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^[\w.-]+\.(com|vn|net|org)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^(hộp quà|set quà|combo quà|combo|set)\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*:\s*(hướng dẫn|huong dan|phân tích|phan tich|tổng hợp|tong hop).*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" -–|")
    return title or "Sản phẩm"


def _refine_product_title(raw_title: str, source_title: str, metadata: dict) -> str:
    title = _clean_title(raw_title)
    source_title = _clean_title(source_title)
    lowered = title.lower()
    if (
        not title
        or title == "Sản phẩm"
        or contains_source_term(title, metadata)
        or any(term in lowered for term in ["http://", "https://", "hướng dẫn", "huong dan", "phân tích", "phan tich", "review", "đánh giá"])
    ):
        return source_title
    source_words = set(re.findall(r"[\wà-ỹ]+", source_title.lower()))
    title_words = set(re.findall(r"[\wà-ỹ]+", lowered))
    overlap = len(source_words & title_words)
    if source_words and overlap < 2:
        return source_title
    if len(title.split()) > 12:
        return source_title
    return title


def _short_product_focus_keyword(title: str, extracted: dict | None = None) -> str:
    specs = extracted.get("product_specs") if isinstance(extracted, dict) else {}
    if isinstance(specs, dict):
        box_name = _clean_title(str(specs.get("box_name") or "")).lower()
        if box_name and 2 <= len(box_name.split()) <= 6:
            return box_name

    cleaned = _clean_title(title).lower()
    for separator in [" – ", " - ", " | ", ":"]:
        if separator in cleaned:
            left, right = cleaned.split(separator, 1)
            if right and any(term in left for term in ["quà", "tặng", "biếu", "người sành", "dành cho"]):
                cleaned = right
                break
    cleaned = re.sub(r"\s*[•|]\s*.*$", "", cleaned).strip()
    cleaned = re.sub(r"\b(cao cấp|chính hãng|giá tốt|quà tặng|làm quà tặng|dành cho|cao cap|hộp quà|set quà|combo quà)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[,;/]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–|")
    use_cases = extracted.get("product_use_cases") if isinstance(extracted, dict) else []
    if cleaned:
        words = cleaned.split()
        if 3 <= len(words) <= 7:
            return cleaned
        if len(words) > 7:
            return " ".join(words[:7])
    if isinstance(use_cases, list):
        for item in use_cases:
            text = _clean_title(str(item)).lower()
            if text and not any(term in text for term in ["quà", "biếu", "tặng"]):
                words = text.split()
                if 3 <= len(words) <= 7:
                    return " ".join(words[:7])
    if specs and any(key in specs for key in ["box_name", "materials", "material"]):
        fallback = " ".join(str(specs.get(key, "")) for key in ["box_name", "materials", "material"]).strip()
        fallback = _clean_title(fallback).lower()
        if fallback:
            return " ".join(fallback.split()[:7])
    return "sản phẩm nổi bật"


def _infer_product_archetype(title: str, extracted: dict | None = None) -> str:
    lowered = _clean_title(title).lower()
    if "trà" in lowered and not any(token in lowered for token in ["bộ", "ấm", "hộp", "set", "quà", "combo"]):
        return "single_tea"
    specs = extracted.get("product_specs") if isinstance(extracted, dict) else {}
    category_hint = str(specs.get("category") or "").lower()
    if "trà" in category_hint and not any(token in category_hint for token in ["quà", "bộ", "ấm"]):
        return "single_tea"
    return "generic_product"


def _product_meta_title(focus_keyword: str) -> str:
    return focus_keyword


def _truncate_meta_title(value: str, limit: int = 60) -> str:
    value = re.sub(r"\s+", " ", value).strip(" -–|:,;")
    if len(value) <= limit:
        return value
    shortened = value[:limit].rsplit(" ", 1)[0].rstrip(" -–|:,;")
    return shortened or value[:limit].rstrip(" -–|:,;")


def _contains_keyword_phrase(value: str, focus_keyword: str) -> bool:
    value = re.sub(r"\s+", " ", (value or "").lower()).strip()
    keyword = re.sub(r"\s+", " ", (focus_keyword or "").lower()).strip()
    if not keyword:
        return True
    if keyword in value:
        return True
    keyword_words = re.findall(r"[\wà-ỹ]+", keyword)
    value_words = set(re.findall(r"[\wà-ỹ]+", value))
    if len(keyword_words) <= 2:
        return all(word in value_words for word in keyword_words)
    matched = sum(1 for word in keyword_words if word in value_words)
    return matched >= max(2, len(keyword_words) - 1)


def _with_focus_keyword(candidate: str, focus_keyword: str) -> str:
    candidate = _clean_title(candidate)
    focus = _clean_title(focus_keyword)
    if not focus or _contains_keyword_phrase(candidate, focus):
        return candidate
    suffix = re.sub(rf"^{re.escape(focus)}\s*[\-|–|:]\s*", "", candidate, flags=re.IGNORECASE).strip()
    joined = f"{focus} | {suffix}" if suffix else focus
    if len(joined) <= 60:
        return joined
    return focus


def _descriptor_candidates(extracted: dict | None) -> list[str]:
    extracted = extracted or {}
    combined = " ".join(
        [
            " ".join(str(item) for item in extracted.get("important_facts", [])[:6]),
            " ".join(str(item) for item in extracted.get("key_points", [])[:6]),
            str((extracted.get("product_specs") or {}).get("package_sizes_text") or ""),
        ]
    ).lower()
    candidates = []
    if any(term in combined for term in ["hậu ngọt", "ngot hau", "ngọt hậu"]):
        candidates.append("hậu ngọt dễ uống")
    if any(term in combined for term in ["mật ong", "trái cây", "hoa quả", "thơm"]):
        candidates.append("hương thơm nổi bật")
    if any(term in combined for term in ["shan", "cổ thụ", "co thu"]):
        candidates.append("đậm chất trà cổ thụ")
    if any(term in combined for term in ["thanh", "êm", "không chát", "khong chat"]):
        candidates.append("thanh vị, dễ uống")
    if not candidates:
        candidates.append("đáng cân nhắc")
    return candidates


def _refine_product_meta_title(raw_meta_title: str, title: str, focus_keyword: str, extracted: dict | None = None) -> str:
    meta_title = _clean_title(raw_meta_title) if str(raw_meta_title or "").strip() else ""
    lowered = meta_title.lower()
    if any(term in lowered for term in ["hướng dẫn", "huong dan", "phân tích", "phan tich", "tổng hợp", "tong hop"]):
        meta_title = ""

    specs = extracted.get("product_specs") if isinstance(extracted, dict) else {}
    package_sizes = str((specs or {}).get("package_sizes_text") or "").strip()
    size_list = [item.strip() for item in package_sizes.split(",") if item.strip()]
    numeric_hint = ""
    if len(size_list) >= 2:
        numeric_hint = f"{len(size_list)} quy cách {', '.join(size_list[:3])}"
    elif size_list:
        numeric_hint = size_list[0]

    awkward_tail = re.search(r"(\||:)\s*([\wà-ỹ]+)$", meta_title, re.IGNORECASE)
    tail_text = awkward_tail.group(2).lower() if awkward_tail else ""
    awkward_single_word = tail_text in {"ngọt", "thanh", "êm", "mượt", "hay", "tốt", "xịn", "ngon"}
    if not meta_title or awkward_single_word:
        meta_title = ""

    candidates = []
    if meta_title:
        normalized = re.sub(r"\s*[:|]\s*", " | ", meta_title)
        normalized = re.sub(r"\s*,\s*,+", ", ", normalized)
        candidates.append(_with_focus_keyword(normalized, focus_keyword))

    title_base = _clean_title(title)
    if not candidates:
        candidates.append(_with_focus_keyword(title_base, focus_keyword))
        if numeric_hint:
            candidates.append(_with_focus_keyword(f"{title_base} | {numeric_hint}", focus_keyword))
        for descriptor in _descriptor_candidates(extracted)[:2]:
            candidates.append(_with_focus_keyword(f"{title_base} | {descriptor}", focus_keyword))
        if focus_keyword and focus_keyword.lower() != title_base.lower():
            candidates.append(_clean_title(focus_keyword.title()))

    cleaned_candidates = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip(" -–|:,;")
        candidate = re.sub(r"\s*[–-]\s*\d+$", "", candidate).rstrip(" -–|:,;")
        candidate = re.sub(r",\s*\d+$", "", candidate).rstrip(" -–|:,;")
        candidate = candidate.replace(" | | ", " | ")
        candidate = _truncate_meta_title(candidate)
        if not candidate:
            continue
        lowered_candidate = candidate.lower()
        if lowered_candidate in seen:
            continue
        seen.add(lowered_candidate)
        cleaned_candidates.append(candidate)

    for candidate in cleaned_candidates:
        if len(candidate) <= 60 and _contains_keyword_phrase(candidate, focus_keyword):
            return candidate
    return _truncate_meta_title(_with_focus_keyword(title_base, focus_keyword))


def _normalize_tag(value: str) -> str:
    tag = _clean_title(str(value or "")).lower()
    tag = re.sub(r"^(tag|thẻ)\s*[:\-]\s*", "", tag, flags=re.IGNORECASE)
    tag = re.sub(r"[#,\.;|]+", " ", tag)
    tag = re.sub(r"\s+", " ", tag).strip(" -–")
    words = tag.split()
    if len(words) > 5:
        tag = " ".join(words[:5])
    return tag


def _fallback_tags(title: str, focus_keyword: str, key_points: list[str], extracted: dict | None) -> list[str]:
    candidates = [focus_keyword, title]
    extracted = extracted or {}
    for key in ["product_use_cases", "important_facts", "key_points"]:
        items = extracted.get(key) if isinstance(extracted, dict) else []
        if isinstance(items, list):
            candidates.extend(str(item) for item in items[:4])
    candidates.extend(str(point) for point in key_points[:4])
    tags = []
    for candidate in candidates:
        tag = _normalize_tag(candidate)
        if 2 <= len(tag) <= 42 and tag not in tags:
            tags.append(tag)
        if len(tags) >= 5:
            break
    return tags[:5]


def _normalize_tags(raw_tags: object, fallback_tags: list[str], focus_keyword: str) -> list[str]:
    values: list[str] = []
    if isinstance(raw_tags, list):
        values = [str(item) for item in raw_tags]
    elif isinstance(raw_tags, str):
        values = [item.strip() for item in re.split(r"[,;\n]", raw_tags) if item.strip()]
    values.extend(fallback_tags)
    if focus_keyword:
        values.insert(0, focus_keyword)
    tags = []
    for value in values:
        tag = _normalize_tag(value)
        if not tag or len(tag) < 2 or len(tag) > 42:
            continue
        if any(term in tag for term in ["http://", "https://", "www.", ".com", ".vn"]):
            continue
        if tag not in tags:
            tags.append(tag)
        if len(tags) >= 5:
            break
    return tags
