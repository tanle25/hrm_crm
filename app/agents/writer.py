from __future__ import annotations

import re
from html import escape, unescape

from app.llm import call_json
from app.source_cleaner import clean_source_object, clean_source_text, source_terms_from_metadata


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
- Không dùng TL;DR hoặc nhãn "Tóm tắt nhanh"; mở bài phải bắt đầu tự nhiên bằng focus keyword trong câu đầu.
- Focus keyword phải xuất hiện tự nhiên trong mở bài, một số H2/H3 và thân bài; tránh viết biến thể quá xa khiến Rank Math không nhận diện được.
- Có ít nhất 3 H2, 1 bảng so sánh nếu phù hợp, phần hỏi đáp tự nhiên, lời kết mềm.
- Không được viết các câu hỏi meta như "Bài viết theo dạng nào?", "Cần những phần nào khi xây dựng nội dung...".
- Không dùng các cụm nhãn nội bộ như SEO, CTA, FAQ, heading, checklist, website/blog trong nội dung người đọc nhìn thấy.
- Phải viết HTML theo template bài blog, không chỉ semantic HTML thô:
  <p>mở bài tự nhiên...</p>
  <div class="content-forge-toc">...</div>
  <section>...các h2/h3/p/ul/table...</section>
  <div class="content-forge-faq">...câu hỏi thường gặp...</div>
  <div class="content-forge-cta">...gợi ý bước tiếp theo...</div>
- TOC phải tóm tắt 3-6 ý chính của bài, không ghi là "TOC".
- FAQ phải dùng câu hỏi thật của người đọc, không hỏi về sản phẩm nếu bài là kiến thức.
- CTA là lời gợi ý mềm theo chủ đề, không gọi tên là CTA.
- Không chèn ảnh, figure hoặc gallery cho bài website; hệ thống sẽ tự chèn ảnh sau.

HTML phải sạch, semantic, dùng các thẻ như: p, h2, h3, ul, li, table, tr, th, td, section, figure, figcaption.
Không thêm giải thích ngoài JSON.
""".strip()


WEBSITE_WRITER_SYSTEM_PROMPT = """
Bạn là biên tập viên SEO tiếng Việt.
Nhiệm vụ: viết bài blog/website theo đúng template HTML được yêu cầu, không viết như trang bán hàng.
Chỉ tối ưu SEO cơ bản: tiêu đề tự nhiên, mở bài rõ ý, heading dễ đọc, từ khóa chính xuất hiện tự nhiên, FAQ hữu ích.
Không dùng thuật ngữ nội bộ như quy trình tối ưu, CTA, checklist, heading trong nội dung người đọc nhìn thấy.
Không dùng TL;DR, không dùng "nội dung trọng tâm", không dùng "thông tin sản phẩm".
Không chèn ảnh, figure, figcaption hoặc gallery.
Không bịa dữ kiện ngoài input.

Trả về JSON hợp lệ với một trường duy nhất: html.

HTML bắt buộc đi theo template này:
<div class="content-forge-article" style="font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;line-height:1.8;color:#333;padding:20px;border:1px solid #eee;border-radius:10px;background-color:#fff;box-sizing:border-box">
  <h1 style="color:{{primary_color}};text-align:center;font-size:28px;margin-bottom:20px;font-weight:bold">...</h1>
  <p style="font-size:16px;margin-bottom:20px">...</p>
  <div class="content-forge-toc" style="background-color:{{soft_color}};padding:20px;border-left:5px solid {{primary_color}};margin-bottom:25px;border-radius:0 8px 8px 0">
    <p style="margin:0;font-weight:bold;color:{{primary_color}};font-size:18px">Nội dung bài viết:</p>
    <ul style="margin:10px 0 0 20px;padding:0;list-style-type:square">...</ul>
  </div>
  <h2 style="color:{{primary_color}};border-bottom:2px solid #e8f5e9;padding-bottom:10px;margin-top:30px">...</h2>
  <p>...</p>
  <ul style="padding-left:20px">...</ul>
  <h2 style="color:{{primary_color}};border-bottom:2px solid #e8f5e9;padding-bottom:10px;margin-top:30px">...</h2>
  <h3 style="color:{{secondary_color}};margin-top:20px;font-size:20px">...</h3>
  <p>...</p>
  <div class="content-forge-faq" style="margin-top:40px;padding:25px;background-color:#fdfdfd;border:1px solid #ddd;border-radius:8px">
    <h2 style="color:{{primary_color}};text-align:center;margin-top:0;margin-bottom:25px">FAQ: Câu Hỏi Thường Gặp</h2>
    <div style="margin-bottom:20px">...</div>
  </div>
  <h2 style="color:{{primary_color}};border-bottom:2px solid #e8f5e9;padding-bottom:10px;margin-top:30px">Kết luận</h2>
  <p>...</p>
  <div class="content-forge-cta" style="text-align:center;margin-top:40px;padding:30px 20px;background:linear-gradient(135deg,{{primary_color}} 0%,{{secondary_color}} 100%);color:white;border-radius:8px;box-shadow:0 4px 15px rgba(0,0,0,0.1)">...</div>
</div>

Quy tắc nội dung:
- H1 là một câu tiêu đề tự nhiên hoàn chỉnh, có từ khóa chính, không dùng dấu ":" "-" "|" để nối vế.
- Mở bài 1 đoạn, câu đầu chứa từ khóa chính và đi thẳng vào nhu cầu người đọc.
- Mục lục có 3-5 gạch đầu dòng thật sự tương ứng với nội dung.
- Thân bài có ít nhất 3 H2; dùng H3 cho danh sách lựa chọn hoặc các mục con.
- FAQ có 3-5 câu hỏi thật của người đọc.
- Kết luận ngắn, mềm, không bán hàng lố.
- CTA mềm, đúng chủ đề, không gọi tên là CTA.
Không thêm giải thích ngoài JSON.
""".strip()

def _clean_phrase(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip(" .,:;|-")


def _site_primary_color(state: dict) -> str:
    color = str((state.get("site_profile") or {}).get("primary_color") or "").strip()
    return color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "#2c5e1a"


def _darken_hex(hex_color: str) -> str:
    value = (hex_color or "#2c5e1a").strip().lstrip("#")
    if len(value) != 6:
        return "#437d28"
    try:
        red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return "#437d28"
    return "#" + "".join(f"{max(0, round(channel * 0.78)):02x}" for channel in (red, green, blue))


def _soft_hex(hex_color: str) -> str:
    value = (hex_color or "#2c5e1a").strip().lstrip("#")
    if len(value) != 6:
        return "#f0f7ed"
    try:
        red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return "#f0f7ed"
    red = round(red + (255 - red) * 0.88)
    green = round(green + (255 - green) * 0.88)
    blue = round(blue + (255 - blue) * 0.88)
    return f"#{red:02x}{green:02x}{blue:02x}"


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


def _source_forbidden_terms(state: dict) -> list[str]:
    metadata = state.get("fetch_result", {}).get("metadata", {}) or {}
    return source_terms_from_metadata(metadata, str(state.get("url") or ""))


def _replace_source_terms(text: str, state: dict, replacement: str = "thông tin sản phẩm") -> str:
    metadata = state.get("fetch_result", {}).get("metadata", {}) or {}
    return clean_source_text(text, metadata, str(state.get("url") or ""), replacement)


def _clean_for_writer(value: object, state: dict) -> object:
    metadata = state.get("fetch_result", {}).get("metadata", {}) or {}
    return clean_source_object(value, metadata, str(state.get("url") or ""))


def _image_library(state: dict) -> list[dict]:
    image_data = state.get("image_data") or {}
    uploaded = image_data.get("uploaded") or []
    images: list[dict] = []
    if uploaded:
        for index, item in enumerate(uploaded[:5], start=1):
            url = _clean_phrase(str(item.get("url", "")))
            alt = _clean_phrase(str(item.get("alt", ""))) or f"{state['plan']['focus_keyword']} - hình {index}"
            if url and "*" not in url and "%2a" not in url.lower():
                images.append({"url": url, "alt": alt})
        return images
    for index, url in enumerate((image_data.get("gallery") or [])[:5], start=1):
        cleaned_url = _clean_phrase(str(url))
        if cleaned_url and "*" not in cleaned_url and "%2a" not in cleaned_url.lower():
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
    if any(term in lower for term in ["nguồn tham khảo", "website nguồn", "url nguồn"]):
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
        if _contains_editorial_meta_text(f"{question} {answer}"):
            continue
        if question and answer:
            blocks.append(f"<h3>{escape(question)}</h3><p>{escape(answer)}</p>")
    if not blocks:
        return html_text
    return html_text + "\n<section><h2>Câu hỏi thường gặp</h2>" + "".join(blocks) + "</section>"


def _contains_editorial_meta_text(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", unescape(value or "")).lower()
    return any(
        marker in normalized
        for marker in [
            "sản phẩm gồm những gì",
            "sản phẩm này phù hợp cho ai",
            "thông số hoặc quy cách đáng chú ý",
            "viết bài seo",
            "tu khoa chính",
            "từ khóa chính",
            "mục tiêu",
            "cấu trúc heading",
            "không viết như trang sản phẩm",
            "woocommerce",
            "website/blog",
            "seo/g",
            "cta",
        ]
    )


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
    source_origin = str(state.get("source_origin") or "").strip().lower()
    source_type = str(state.get("fetch_result", {}).get("metadata", {}).get("source_type") or "").strip().lower()
    if source_origin in {"website_keyword", "website_article_url"} or source_type == "article":
        return html_text
    return _replace_source_terms(html_text, state, replacement="thương hiệu")


def _sanitize_website_html(html_text: str) -> str:
    sanitized = re.sub(r"<figure\b[^>]*>.*?</figure>\s*", "", html_text or "", flags=re.IGNORECASE | re.DOTALL)
    sanitized = re.sub(r"<img\b[^>]*>\s*", "", sanitized, flags=re.IGNORECASE | re.DOTALL)
    sanitized = re.sub(
        r"<section\b[^>]*(?:content-forge-image-grid|content-forge-inline-gallery|content-forge-affiliate-gallery)[^>]*>.*?</section>\s*",
        "",
        sanitized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    bad_phrases = [
        r"[^.!?。]*\bnội dung trọng tâm\b[^.!?。]*[.!?。]?\s*",
        r"[^.!?。]*\bthông tin sản phẩm\b[^.!?。]*[.!?。]?\s*",
        r"[^.!?。]*\bAI[- ]?friendly\b[^.!?。]*[.!?。]?\s*",
        r"[^.!?。]*\bGEO\b[^.!?。]*[.!?。]?\s*",
    ]
    for pattern in bad_phrases:
        sanitized = re.sub(pattern, " ", sanitized, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s{2,}", " ", sanitized).strip()


def _website_html_valid(html_text: str) -> bool:
    lowered = (html_text or "").lower()
    return all(
        marker in lowered
        for marker in [
            "content-forge-article",
            "content-forge-toc",
            "content-forge-faq",
            "content-forge-cta",
            "<h1",
            "<h2",
        ]
    )


def _infer_product_archetype(state: dict) -> str:
    title = str(state.get("plan", {}).get("title") or state.get("fetch_result", {}).get("title") or "").lower()
    if "trà" in title and not any(token in title for token in ["bộ", "ấm", "hộp", "set", "quà", "combo"]):
        return "single_tea"
    return "generic_product"


def run(state: dict) -> dict:
    fallback = {"html": ""}
    extracted = state["extracted"]
    source_origin = str(state.get("source_origin") or "").strip().lower()
    metadata_source_type = str(state["fetch_result"].get("metadata", {}).get("source_type") or "").strip().lower()
    is_product = (
        metadata_source_type == "product"
        or (state.get("plan", {}).get("schema_type") == "Product")
        or (state.get("plan", {}).get("article_type") == "Product Description")
    )
    image_library = _image_library(state)
    concise_extracted = {
        "product_components": extracted.get("product_components", []),
        "product_specs": extracted.get("product_specs", {}),
        "product_use_cases": extracted.get("product_use_cases", []),
        "buyer_objections": extracted.get("buyer_objections", []),
        "faq_items": extracted.get("faq_items", [])[:4],
        "component_profiles": extracted.get("component_profiles", {}),
    }
    if is_product:
        concise_extracted = _clean_for_writer(concise_extracted, state)
    archetype = _infer_product_archetype(state)
    concise_metadata = {
        "title": _replace_source_terms(str(state["fetch_result"].get("title") or ""), state) if is_product else str(state["fetch_result"].get("title") or ""),
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
    if is_product:
        concise_plan = _clean_for_writer(concise_plan, state)
    source_excerpt_raw = str(state["fetch_result"].get("clean_content") or "")
    source_excerpt = (_replace_source_terms(source_excerpt_raw, state) if is_product else source_excerpt_raw)[:1800]
    knowledge_limit = 8 if source_origin in {"website_keyword", "website_article_url"} else 4
    raw_knowledge_facts = state.get("knowledge_facts", [])[:knowledge_limit]
    knowledge_facts = _clean_for_writer(raw_knowledge_facts, state) if is_product else raw_knowledge_facts
    knowledge_instruction = (
        "Với bài viết theo keyword, hãy lấy kiến thức RAG làm nguồn chính; chỉ dùng brief keyword để định hướng intent, không coi brief là nguồn dữ kiện đầy đủ.\n"
        "Nếu knowledge facts chứa hướng dẫn tối ưu nội dung, hãy dùng như chỉ dẫn biên tập nội bộ, không trích nguyên văn và không đưa thuật ngữ kỹ thuật như CTA, FAQ, heading, checklist vào nội dung hiển thị.\n"
        if source_origin == "website_keyword"
        else "Với bài viết từ URL, hãy kết hợp dữ kiện nguồn và knowledge facts từ RAG; ưu tiên nguồn khi có xung đột.\n"
        "Nếu knowledge facts chứa hướng dẫn tối ưu nội dung, hãy dùng như chỉ dẫn biên tập nội bộ, không trích nguyên văn và không đưa thuật ngữ kỹ thuật như CTA, FAQ, heading, checklist vào nội dung hiển thị.\n"
        if source_origin == "website_article_url"
        else ""
    )
    website_template_instruction = ""
    if not is_product:
        website_template_instruction = (
            "TEMPLATE HTML BẮT BUỘC CHO BÀI WEBSITE:\n"
            "1) Mở bằng đúng 1 thẻ <h1> tự nhiên, sau đó là đúng 1 thẻ <p> mở bài; câu đầu của mở bài chứa focus keyword. Không viết 'nội dung trọng tâm', không viết 'thông tin sản phẩm'.\n"
            "2) Sau mở bài là <div class=\"content-forge-toc\"><p>Nội dung bài viết:</p><ul>...</ul></div> với 3-6 ý chính.\n"
            "3) Thân bài chia thành nhiều <section>, mỗi section có <h2> và 1-4 đoạn/bullet/table. H2 không dùng số thứ tự nếu không cần.\n"
            "4) Nếu có danh sách, dùng <ul><li>...</li></ul>. Nếu so sánh, dùng <table><tr><th>...</th></tr>...</table>.\n"
            "5) FAQ dùng <div class=\"content-forge-faq\"><h2>FAQ: Câu Hỏi Thường Gặp</h2><div><h3>...</h3><p>...</p></div>...</div>.\n"
            "6) Kết thúc bằng <div class=\"content-forge-cta\"><p>...</p><p>...</p><a href=\"#\">...</a></div>.\n"
            "7) Phải có đúng 1 <h1> ở đầu template; không chèn ảnh, figure, figcaption hoặc class content-forge-image-grid.\n"
        )
    primary_color = _site_primary_color(state)
    secondary_color = _darken_hex(primary_color)
    soft_color = _soft_hex(primary_color)
    prompt = (
        f"Metadata: {concise_metadata}\n"
        f"Plan: {concise_plan}\n"
        f"Product archetype: {archetype}\n"
        f"Content mode: {state.get('content_mode') or 'shared'}\n"
        f"Site profile: {state.get('site_profile') or {}}\n"
        f"Concise extracted data: {concise_extracted}\n"
        f"Uploaded/local image library: {image_library if is_product else []}\n"
        f"Knowledge facts: {knowledge_facts}\n"
        f"Source/brief excerpt: {source_excerpt}\n"
        f"Template colors: primary_color={primary_color}, secondary_color={secondary_color}, soft_color={soft_color}\n"
        f"{knowledge_instruction}"
        f"{website_template_instruction}"
        "Yêu cầu: tiếng Việt tự nhiên, có quan sát thực tế, không sáo rỗng, không bịa dữ kiện.\n"
        "Không nhắc website nguồn, URL nguồn hoặc thương hiệu nguồn trong nội dung cuối.\n"
        "Không dùng blockquote mở đầu, không dùng heading kiểu 'Gợi ý nhanh' hay 'Mô tả ngắn'.\n"
        "Mở bài đi thẳng vào bối cảnh thực tế; thân bài mềm mại; kết bài gợi bước tiếp theo thật tự nhiên, không gọi tên là CTA.\n"
        "Không kéo toàn bộ câu chuyện sang quà biếu nếu dữ liệu không cho thấy đó là trung tâm.\n"
        "Heading phải tự nhiên, không đều tay kiểu slogan; phần hỏi đáp phải là băn khoăn thật, không hỏi về cấu trúc bài viết hoặc SEO.\n"
        + ("Dựa vào ảnh để mô tả hình thức sản phẩm; HTML cuối cần có 3-5 ảnh chèn tự nhiên trong thân bài.\n" if is_product else "Bài website phải tự sinh TOC, FAQ card và CTA theo class template ở trên; không chèn ảnh, figure, figcaption hoặc gallery.\n")
        + ("Không dùng cụm 'thông tin sản phẩm' nếu đây là bài kiến thức/blog; hãy gọi đúng chủ đề bằng focus keyword hoặc biến thể tự nhiên.\n" if not is_product else "")
        +
        "Focus keyword rải tự nhiên ở mở bài, vài heading, bảng/bullet, FAQ, caption ảnh và kết bài; ưu tiên mật độ khoảng 1-1.5% tính trên toàn bài, dùng cả exact phrase và biến thể gần nhưng không nhồi máy móc.\n"
        "Để tránh density thấp, exact focus keyword nên xuất hiện tối thiểu khoảng 0.8% số từ: bài 1500 từ cần ít nhất 12 lần, bài 2000 từ cần ít nhất 16 lần, bài 2500 từ cần ít nhất 20 lần; hãy rải đều và tự nhiên.\n"
        "Độ dài mục tiêu cho product: 1500-2500 chữ. Nếu archetype là single_tea, ưu tiên nửa dưới của khoảng này và tập trung vào hương, vị, nước trà, cánh trà, cách pha, đối tượng hợp gu, lý do chọn loại trà này.\n"
    )
    max_tokens = 2600 if archetype == "single_tea" else 3200
    system_prompt = WRITER_SYSTEM_PROMPT
    if not is_product:
        system_prompt = (
            WEBSITE_WRITER_SYSTEM_PROMPT
            .replace("{{primary_color}}", primary_color)
            .replace("{{secondary_color}}", secondary_color)
            .replace("{{soft_color}}", soft_color)
        )
        max_tokens = 4200
    data = call_json("writer", system_prompt, prompt, fallback=fallback, max_tokens=max_tokens)
    data_html = _coerce_text_field(data.get("html"))
    if not data_html:
        raise RuntimeError("Writer returned empty html.")
    if image_library and is_product:
        data_html = _inject_inline_images(data_html, image_library, state["plan"]["focus_keyword"])
    if is_product:
        data_html = _append_faq_if_missing(data_html, extracted.get("faq_items", []))
    data_html = _sanitize_product_terms(data_html)
    data_html = _sanitize_source_terms(data_html, state)
    if not is_product:
        data_html = _sanitize_website_html(data_html)
        if not _website_html_valid(data_html):
            raise RuntimeError("Writer returned website html that did not follow the required article template.")
    if is_product:
        validation_error = _product_html_validation_error(data_html)
        if validation_error:
            raise RuntimeError(f"Writer returned product html that did not pass structural validation: {validation_error}")
    return {"html": data_html}
