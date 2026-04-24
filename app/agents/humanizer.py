from __future__ import annotations

from app.llm import call_json


HUMANIZER_SYSTEM_PROMPT = """
Bạn là biên tập viên tiếng Việt.
Làm bài viết tự nhiên hơn nhưng không bịa thêm thông tin.
Ưu tiên câu ngắn, chuyển đoạn mượt, giọng tư vấn chuyên nghiệp, không lặp ý.
Nếu là product, hãy biên tập theo phong cách landing page bán hàng cao cấp:
- mở bài mềm, giàu hình ảnh, chạm vào đúng bối cảnh mua hoặc sử dụng sản phẩm
- đoạn thân có nhịp kể chuyện và sức thuyết phục, không khô như báo cáo
- giữ cảm giác sang trọng, tinh tế, tránh cường điệu lố hoặc sáo rỗng
Loại bỏ cách viết máy móc kiểu "nội dung dưới đây", "bài viết này", "nguồn gốc".
Không nhắc tên website nguồn, URL nguồn hoặc thương hiệu nguồn.
Giữ nguyên toàn bộ các heading, table, image, list, section hiện có, không được bỏ bớt phần nào.
Không rút gọn bài, không cắt phần cuối, không gộp mất FAQ/checklist/review/xem thêm.
Nếu có site_profile và content_mode = per-site, hãy làm câu chữ hợp hơn với tone của site đó nhưng không đổi dữ kiện.
Trả về JSON hợp lệ với một trường duy nhất: html.
Không thêm giải thích.
""".strip()


def _heuristic_humanize(html_text: str, source_type: str | None = None) -> dict:
    return {"html": html_text}


def run(html_text: str, source_type: str | None = None, site_profile: dict | None = None, content_mode: str | None = None) -> dict:
    fallback = _heuristic_humanize(html_text, source_type)
    prompt = (
        f"Source type: {source_type or ''}\n"
        f"Content mode: {content_mode or 'shared'}\n"
        f"Site profile: {site_profile or {}}\n"
        f"HTML hien tai:\n{html_text[:6500]}\n\n"
        "Yêu cầu: giữ nguyên cấu trúc heading/bảng/checklist/FAQ/review/xem thêm và hình ảnh, chỉ làm câu chữ tự nhiên hơn, tuyệt đối không rút gọn hay bỏ section.\n"
        "Nếu là product, ưu tiên giọng tư vấn bán hàng tinh tế như trang giới thiệu sản phẩm cao cấp, không biến thành bài thông số khô cứng."
    )
    data = call_json("humanizer", HUMANIZER_SYSTEM_PROMPT, prompt, fallback=fallback, max_tokens=2600)
    html_value = data.get("html")
    if not isinstance(html_value, str) or not html_value:
        return fallback
    lowered = html_value.lower()
    required_sections = ["faq"]
    if source_type == "product":
        required_sections.extend(["đánh giá và cảm nhận", "xem thêm"])
    if any(section not in lowered for section in required_sections):
        return {"html": html_text}
    if "<img " in html_text.lower() and "<img " not in lowered:
        html_value = html_text
    return {"html": html_value}
