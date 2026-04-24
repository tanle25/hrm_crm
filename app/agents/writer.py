from __future__ import annotations

import re
import unicodedata
from html import escape, unescape
from urllib.parse import urlparse

from app.llm import call_json


WRITER_SYSTEM_PROMPT = """
Bạn là biên tập viên thương mại điện tử tiếng Việt.
Viết tự nhiên, có dấu tiếng Việt đầy đủ, câu rõ ý, không dùng giọng máy móc.
Trả về JSON hợp lệ với một trường duy nhất:
html.

Nếu nguồn là product:
- Viết như trang sản phẩm bán hàng chất lượng, không chép lại nguồn.
- Hãy tự chọn bố cục phù hợp với chính sản phẩm đang có, không áp template cứng cho mọi sản phẩm.
- Chỉ dùng những gì đã có trong metadata, extracted data, knowledge facts và image library.
- Phải phân biệt nguồn article/product; nếu là product thì phân biệt product_kind simple/variable trong metadata.
- Với product variable, hãy giải thích các biến thể/quy cách và cách chọn tự nhiên theo dữ liệu thật.
- Với product simple, không tự bịa biến thể hay bảng so sánh biến thể.
- Không được ghi các nhãn kỹ thuật như "product variable", "product_kind", "simple product" trong nội dung; hãy diễn đạt tự nhiên như "sản phẩm có nhiều quy cách" hoặc "một sản phẩm chính".
- Văn phong phải cuốn hút như một landing page bán hàng cao cấp:
  mở bài có chất kể chuyện nhẹ, chạm đúng bối cảnh mua hoặc chọn sản phẩm; thân bài mềm mại, tránh cảm giác checklist máy móc; kết lại có đoạn kêu gọi hành động tinh tế.
- Khi viết lợi ích, không chỉ liệt kê thông số mà phải diễn giải lợi ích mua hàng thực tế đúng với sản phẩm đang có.
- Nếu phù hợp với dữ liệu và loại sản phẩm, nên có bảng hoặc bullet block để giúp người đọc quét thông tin nhanh; không được chèn cho có nếu làm bài gượng.
- Nếu extracted data có faq_items hoặc buyer_objections, nội dung product nên có phần FAQ rõ ràng ở nửa sau bài.
- Không bịa giá, trọng lượng, khuyến mãi, chứng nhận hoặc cam kết nếu nguồn không có.
- Ưu tiên từ khóa tự nhiên, không nhồi keyword.
- Không nhắc tên website nguồn, thương hiệu nguồn, URL nguồn, "nguồn tham khảo".
- Viết như người bán/biên tập viên độc lập, không viết như bản tóm tắt dữ liệu.
- Nếu input có site_profile và content_mode = per-site, hãy điều chỉnh giọng văn, ví dụ, nhịp mô tả và cảm giác thương hiệu để hợp với site đó.
- Nếu input có primary_color của site và content_mode = per-site, xem đó là định hướng thẩm mỹ ngầm cho cách diễn đạt, không được nhắc mã màu trong nội dung.

Nếu không phải product:
- Có TL;DR, ít nhất 3 H2, 1 bảng so sánh, FAQ, kết bài có tác giả/ngày/nguồn.

HTML phải sạch, semantic, dùng các thẻ như: p, h2, h3, ul, li, table, tr, th, td, section, figure, figcaption.
Không thêm giải thích ngoài JSON.
""".strip()

def _clean_phrase(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip(" .,:;|-")


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ["summary", "description", "note", "text", "profile"]:
            if isinstance(value.get(key), str):
                return value[key]
        return " ".join(str(item) for item in value.values() if isinstance(item, (str, int, float)))
    if isinstance(value, list):
        return " ".join(str(item) for item in value if isinstance(item, (str, int, float)))
    return "" if value is None else str(value)


def _coerce_text_field(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ["markdown", "html", "content", "text", "body", "value"]:
            if isinstance(value.get(key), str):
                return value[key]
        return _as_text(value)
    if isinstance(value, list):
        parts = [_as_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    return _as_text(value)


def _html_to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def _source_forbidden_terms(state: dict) -> list[str]:
    metadata = state.get("fetch_result", {}).get("metadata", {}) or {}
    source_url = str(metadata.get("url") or state.get("url") or "")
    parsed = urlparse(source_url)
    host = parsed.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    candidates: list[str] = []
    if host:
        candidates.append(host)
        candidates.extend(part for part in re.split(r"[^a-z0-9-]+", host) if part)
    for key in ["sitename", "author", "site_name", "publisher"]:
        value = metadata.get(key)
        if isinstance(value, str):
            candidates.append(value)
    terms: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        term = _clean_phrase(str(candidate)).lower()
        term = re.sub(r"\b(com|vn|net|org|www)\b", " ", term)
        term = re.sub(r"\s+", " ", term).strip(" .,:;|-")
        if len(term) < 4:
            continue
        compact = re.sub(r"[^a-z0-9à-ỹ]+", "", term)
        ascii_compact = re.sub(r"[^a-z0-9]+", "", _strip_accents(term).lower())
        for item in {term, compact, ascii_compact}:
            if len(item) >= 4 and item not in seen:
                seen.add(item)
                terms.append(item)
    return terms


def _replace_source_terms(text: str, state: dict, replacement: str = "thông tin sản phẩm") -> str:
    cleaned = re.sub(r"https?://\S+", "", text or "", flags=re.IGNORECASE)
    for term in _source_forbidden_terms(state):
        if not term:
            continue
        if re.fullmatch(r"[a-z0-9-]+", term):
            pattern = rf"\b{re.escape(term)}\b"
        else:
            pattern = re.escape(term)
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(website|url)\s+nguồn\b", replacement, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bnguồn\s+tham\s+khảo\b", "thông tin tham khảo", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def _clean_for_writer(value: object, state: dict) -> object:
    if isinstance(value, str):
        return _replace_source_terms(value, state)
    if isinstance(value, list):
        return [_clean_for_writer(item, state) for item in value]
    if isinstance(value, dict):
        blocked_keys = {"url", "canonical_url", "source_url", "sitename", "author", "publisher", "site_name"}
        cleaned: dict = {}
        for key, item in value.items():
            if str(key).lower() in blocked_keys:
                continue
            cleaned[key] = _clean_for_writer(item, state)
        return cleaned
    return value


def _image_library(state: dict) -> list[dict]:
    image_data = state.get("image_data") or {}
    uploaded = image_data.get("uploaded") or []
    images: list[dict] = []
    if uploaded:
        for index, item in enumerate(uploaded[:5], start=1):
            url = _clean_phrase(str(item.get("url", "")))
            alt = _clean_phrase(str(item.get("alt", ""))) or f"{state['plan']['focus_keyword']} - hình {index}"
            if url:
                images.append({"url": url, "alt": alt})
        return images
    for index, url in enumerate((image_data.get("gallery") or [])[:5], start=1):
        cleaned_url = _clean_phrase(str(url))
        if cleaned_url:
            images.append({"url": cleaned_url, "alt": f"{state['plan']['focus_keyword']} - hình {index}"})
    return images


def _insert_after_heading(html: str, heading: str, block: str) -> str:
    pattern = re.compile(rf"(<h2[^>]*>\s*{re.escape(heading)}\s*</h2>\s*(?:<p[^>]*>.*?</p>)?)", re.IGNORECASE | re.DOTALL)
    match = pattern.search(html)
    if not match:
        return html
    return html[: match.end()] + "\n" + block + html[match.end() :]


def _insert_after_nth_h2(html: str, index: int, block: str) -> str:
    matches = list(re.finditer(r"(<h2[^>]*>.*?</h2>\s*(?:<p[^>]*>.*?</p>)?)", html, re.IGNORECASE | re.DOTALL))
    if index < 0 or index >= len(matches):
        return html
    match = matches[index]
    return html[: match.end()] + "\n" + block + html[match.end() :]


def _inject_inline_images(html: str, image_entries: list[dict], focus_keyword: str) -> str:
    html = _remove_invalid_inline_images(html or "")
    html = re.sub(r"<figure>\s*</figure>", "", html, flags=re.IGNORECASE)
    if not image_entries:
        return html
    if _has_valid_inline_image(html):
        return html
    html = re.sub(r"<section\b[^>]*content-forge-inline-gallery[^>]*>\s*</section>\s*", "", html, flags=re.IGNORECASE | re.DOTALL)
    if "<img " in html.lower():
        return html
    selected = image_entries[:5]
    placements = [
        ("Tổng quan sản phẩm", "Hình ảnh tổng quan sản phẩm"),
        ("Thông số kỹ thuật", "Chi tiết hoàn thiện và cấu tạo sản phẩm"),
        ("Hướng dẫn sử dụng", "Gợi ý sử dụng sản phẩm trong bối cảnh thực tế"),
        ("Bảo quản", "Chi tiết bề mặt và tình trạng hoàn thiện"),
    ]
    updated = html
    used = 0
    for heading, caption in placements:
        if used >= len(selected):
            break
        image = selected[used]
        block = (
            '<section class="content-forge-inline-gallery">'
            f'<figure><img src="{escape(image["url"], quote=True)}" alt="{escape(image["alt"], quote=True)}" loading="lazy" '
            'style="width:100%;height:auto;border-radius:14px;display:block" />'
            f'<figcaption>{escape(caption)}. {escape(focus_keyword)}</figcaption></figure>'
            "</section>"
        )
        injected = _insert_after_heading(updated, heading, block)
        if injected == updated:
            injected = _insert_after_nth_h2(updated, used, block)
        if injected != updated:
            updated = injected
            used += 1
    if used == 0:
        figures = []
        for index, image in enumerate(selected[:3], start=1):
            figures.append(
                f'<figure><img src="{escape(image["url"], quote=True)}" alt="{escape(image["alt"], quote=True)}" loading="lazy" '
                'style="width:100%;height:auto;border-radius:14px;display:block" />'
                f'<figcaption>Hình ảnh tham chiếu #{index} cho {escape(focus_keyword)}</figcaption></figure>'
            )
        return '<section class="content-forge-inline-gallery">' + "".join(figures) + "</section>\n" + html
    return updated


def _has_valid_inline_image(html: str) -> bool:
    for match in re.finditer(r"<img\b[^>]*>", html or "", flags=re.IGNORECASE | re.DOTALL):
        src_match = re.search(r"""\bsrc\s*=\s*(['"])(.*?)\1""", match.group(0), flags=re.IGNORECASE | re.DOTALL)
        if src_match and re.match(r"^https?://", src_match.group(2).strip(), flags=re.IGNORECASE):
            return True
    return False


def _remove_invalid_inline_images(html: str) -> str:
    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        src_match = re.search(r"""\bsrc\s*=\s*(['"])(.*?)\1""", tag, flags=re.IGNORECASE | re.DOTALL)
        if not src_match:
            return ""
        src = _clean_phrase(src_match.group(2))
        if not re.match(r"^https?://", src, flags=re.IGNORECASE):
            return ""
        return tag

    cleaned = re.sub(r"<img\b[^>]*>", replace, html or "", flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<figure[^>]*>\s*(?:<figcaption[^>]*>.*?</figcaption>\s*)?</figure>\s*", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned


def _product_html_validation_error(html_text: str) -> str | None:
    lower = html_text.lower()
    if "<section" not in lower and "<p" not in lower:
        return "missing semantic html blocks"
    if any(term in lower for term in ["traviet", "trà việt", "nguồn tham khảo", "website nguồn", "url nguồn"]):
        return "mentions source site"
    if any(term in lower for term in ["chưa xác nhận từ dữ liệu nguồn", "chưa thấy nêu rõ trong dữ liệu nguồn"]):
        return "contains uncertain-source disclaimer"
    if any(term in lower for term in ["product variable", "product_kind", "simple product", "variable product"]):
        return "contains technical product labels"
    word_count = len(_html_to_text(html_text).split())
    if word_count < 350:
        return f"too short: {word_count} words"
    return None


def _product_html_valid(html_text: str) -> bool:
    return _product_html_validation_error(html_text) is None


def _append_faq_if_missing(html_text: str, faq_items: list[dict]) -> str:
    if not faq_items:
        return html_text
    lowered = html_text.lower()
    if "câu hỏi thường gặp" in lowered or ">faq<" in lowered:
        return html_text
    blocks = []
    for item in faq_items[:5]:
        if not isinstance(item, dict):
            continue
        question = _clean_phrase(str(item.get("question") or ""))
        answer = _clean_phrase(str(item.get("answer") or ""))
        if question and answer:
            blocks.append(f"<h3>{escape(question)}</h3><p>{escape(answer)}</p>")
    if not blocks:
        return html_text
    return html_text + "\n<section><h2>Câu hỏi thường gặp</h2>" + "".join(blocks) + "</section>"


def _sanitize_product_terms(html_text: str) -> str:
    replacements = {
        r"\bproduct\s+variable\b": "sản phẩm",
        r"\bvariable\s+product\b": "sản phẩm",
        r"\bsimple\s+product\b": "một sản phẩm chính",
        r"\bproduct_kind\b": "loại sản phẩm",
        r"website\s+nguồn": "thông tin sản phẩm",
        r"nguồn\s+tham\s+khảo": "thông tin tham khảo",
        r"url\s+nguồn": "đường dẫn sản phẩm",
    }
    sanitized = html_text
    for pattern, replacement in replacements.items():
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
    return sanitized


def _sanitize_source_terms(html_text: str, state: dict) -> str:
    return _replace_source_terms(html_text, state, replacement="thương hiệu")


def _infer_product_archetype(state: dict) -> str:
    title = str(state.get("plan", {}).get("title") or state.get("fetch_result", {}).get("title") or "").lower()
    if "trà" in title and not any(token in title for token in ["bộ", "ấm", "hộp", "set", "quà", "combo"]):
        return "single_tea"
    return "generic_product"


def run(state: dict) -> dict:
    fallback = {"html": ""}
    extracted = state["extracted"]
    image_library = _image_library(state)
    concise_extracted = {
        "product_components": extracted.get("product_components", []),
        "product_specs": extracted.get("product_specs", {}),
        "product_use_cases": extracted.get("product_use_cases", []),
        "buyer_objections": extracted.get("buyer_objections", []),
        "faq_items": extracted.get("faq_items", [])[:4],
        "component_profiles": extracted.get("component_profiles", {}),
    }
    concise_extracted = _clean_for_writer(concise_extracted, state)
    archetype = _infer_product_archetype(state)
    concise_metadata = {
        "title": _replace_source_terms(str(state["fetch_result"].get("title") or ""), state),
        "source_type": state["fetch_result"].get("metadata", {}).get("source_type"),
        "product_kind": state["fetch_result"].get("metadata", {}).get("product_kind"),
        "product_hints": {
            key: _clean_for_writer(state["fetch_result"].get("metadata", {}).get("product_hints", {}).get(key), state)
            for key in ["meta_description", "price_text", "sku", "category", "weight_text"]
            if state["fetch_result"].get("metadata", {}).get("product_hints", {}).get(key)
        },
    }
    concise_plan = {
        "title": state["plan"].get("title"),
        "focus_keyword": state["plan"].get("focus_keyword"),
        "meta_title": state["plan"].get("meta_title"),
        "outline": state["plan"].get("outline"),
        "article_type": state["plan"].get("article_type"),
        "schema_type": state["plan"].get("schema_type"),
        "product_kind": state["plan"].get("product_kind") or state["fetch_result"].get("metadata", {}).get("product_kind"),
    }
    concise_plan = _clean_for_writer(concise_plan, state)
    source_excerpt = _replace_source_terms(str(state["fetch_result"].get("clean_content") or ""), state)[:1800]
    knowledge_facts = _clean_for_writer(state.get("knowledge_facts", [])[:4], state)
    prompt = (
        f"Metadata: {concise_metadata}\n"
        f"Plan: {concise_plan}\n"
        f"Product archetype: {archetype}\n"
        f"Content mode: {state.get('content_mode') or 'shared'}\n"
        f"Site profile: {state.get('site_profile') or {}}\n"
        f"Concise extracted data: {concise_extracted}\n"
        f"Uploaded/local image library (3-5 ảnh để dùng tự nhiên trong bài): {image_library}\n"
        f"Knowledge facts: {knowledge_facts}\n"
        f"Clean product excerpt: {source_excerpt}\n"
        "Yêu cầu: tiếng Việt tự nhiên, có quan sát thực tế, không sáo rỗng, không bịa dữ kiện.\n"
        "Không nhắc website nguồn, URL nguồn hoặc thương hiệu nguồn trong nội dung cuối.\n"
        "Không dùng blockquote mở đầu, không dùng heading kiểu 'Gợi ý nhanh' hay 'Mô tả ngắn'.\n"
        "Mở bài đi thẳng vào bối cảnh mua hoặc dùng thực tế; thân bài mềm mại; kết bài có CTA tinh tế.\n"
        "Không kéo toàn bộ câu chuyện sang quà biếu nếu dữ liệu không cho thấy đó là trung tâm.\n"
        "Heading phải tự nhiên, không đều tay kiểu slogan; FAQ phải là băn khoăn mua hàng thật.\n"
        "Dựa vào ảnh để mô tả hình thức sản phẩm; HTML cuối cần có 3-5 ảnh chèn tự nhiên trong thân bài.\n"
        "Focus keyword rải tự nhiên ở mở bài, vài heading, bảng/bullet, FAQ, caption ảnh và kết bài; ưu tiên mật độ khoảng 1-1.5% tính trên toàn bài, dùng cả exact phrase và biến thể gần nhưng không nhồi máy móc.\n"
        "Để tránh density thấp, exact focus keyword nên xuất hiện tối thiểu khoảng 0.8% số từ: bài 1500 từ cần ít nhất 12 lần, bài 2000 từ cần ít nhất 16 lần, bài 2500 từ cần ít nhất 20 lần; hãy rải đều và tự nhiên.\n"
        "Độ dài mục tiêu cho product: 1500-2500 chữ. Nếu archetype là single_tea, ưu tiên nửa dưới của khoảng này và tập trung vào hương, vị, nước trà, cánh trà, cách pha, đối tượng hợp gu, lý do chọn loại trà này.\n"
    )
    max_tokens = 2600 if archetype == "single_tea" else 3200
    data = call_json("writer", WRITER_SYSTEM_PROMPT, prompt, fallback=fallback, max_tokens=max_tokens)
    data_html = _coerce_text_field(data.get("html"))
    if not data_html:
        raise RuntimeError("Writer returned empty html.")
    if image_library:
        data_html = _inject_inline_images(data_html, image_library, state["plan"]["focus_keyword"])
    data_html = _append_faq_if_missing(data_html, extracted.get("faq_items", []))
    data_html = _sanitize_product_terms(data_html)
    data_html = _sanitize_source_terms(data_html, state)
    is_product = (
        (state["fetch_result"]["metadata"].get("source_type") or "").lower() == "product"
        or (state.get("plan", {}).get("schema_type") == "Product")
        or (state.get("plan", {}).get("article_type") == "Product Description")
    )
    if is_product:
        validation_error = _product_html_validation_error(data_html)
        if validation_error:
            raise RuntimeError(f"Writer returned product html that did not pass structural validation: {validation_error}")
    return {"html": data_html}
