from __future__ import annotations

from app.llm import call_json


CLASSIFIER_SYSTEM_PROMPT = """
Bạn là Source Classifier cho pipeline nội dung tiếng Việt.
Nhiệm vụ: đọc evidence ngắn từ URL/HTML/metadata và tự phân loại nguồn.

Trả về JSON object hợp lệ:
{
  "source_type": "product" | "article",
  "product_kind": "simple" | "variable" | "",
  "confidence": 0.0-1.0,
  "reason": "ngắn gọn"
}

Quy tắc:
- product: trang bán sản phẩm, có giá, SKU, giỏ hàng, biến thể, product schema, WooCommerce product, hoặc copy mua hàng.
- article: bài viết/tin tức/hướng dẫn/blog, không phải trang mua sản phẩm chính.
- product_kind chỉ dùng khi source_type là product.
- simple: một sản phẩm chính không có lựa chọn biến thể/quy cách bắt buộc.
- variable: có lựa chọn biến thể/quy cách/màu/size/trọng lượng/hương vị/option mua khác nhau.
- Nếu không chắc product_kind thì chọn simple với confidence thấp hơn, không bịa biến thể.
Không thêm giải thích ngoài JSON.
""".strip()


def run(evidence: dict, fallback: dict | None = None) -> dict:
    fallback = fallback or {
        "source_type": "article",
        "product_kind": "",
        "confidence": 0.0,
        "reason": "fallback",
    }
    data = call_json(
        "classifier",
        CLASSIFIER_SYSTEM_PROMPT,
        f"Evidence:\n{evidence}",
        fallback=fallback,
        max_tokens=300,
    )
    source_type = str(data.get("source_type") or fallback.get("source_type") or "article").lower()
    if source_type not in {"product", "article"}:
        source_type = fallback.get("source_type", "article")
    product_kind = str(data.get("product_kind") or "").lower()
    if source_type != "product":
        product_kind = ""
    elif product_kind not in {"simple", "variable"}:
        product_kind = fallback.get("product_kind") or "simple"
    try:
        confidence = float(data.get("confidence", fallback.get("confidence", 0.0)))
    except (TypeError, ValueError):
        confidence = float(fallback.get("confidence", 0.0))
    return {
        "source_type": source_type,
        "source_kind": source_type,
        "product_kind": product_kind,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(data.get("reason") or fallback.get("reason") or "").strip()[:240],
    }
