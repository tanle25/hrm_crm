from __future__ import annotations

import json

from dotenv import load_dotenv

from app.agents import classifier, publisher, writer


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_classifier() -> dict:
    article = classifier.run(
        {
            "url": "https://example.com/blog/cach-pha-tra",
            "title": "Cách pha trà xanh ngon tại nhà",
            "wp_type": "post",
            "meta_description": "Bài viết hướng dẫn cách pha trà xanh.",
            "price_text": "",
            "sku": "",
            "variants": [],
            "product_markers": [],
            "article_markers": ["article", "blogposting"],
            "visible_excerpt": "Bài viết chia sẻ kinh nghiệm pha trà xanh, nhiệt độ nước và thời gian hãm.",
        },
        fallback={"source_type": "article", "product_kind": "", "confidence": 0.7, "reason": "article markers"},
    )
    simple = classifier.run(
        {
            "url": "https://example.com/product/am-tra",
            "title": "Ấm trà thủy tinh 500ml",
            "wp_type": "product",
            "meta_description": "Trang bán một ấm trà thủy tinh.",
            "price_text": "250.000đ",
            "sku": "AM-500",
            "variants": [],
            "product_markers": ["woocommerce-product", "add_to_cart"],
            "article_markers": [],
            "visible_excerpt": "Ấm trà thủy tinh 500ml, thêm vào giỏ hàng.",
        },
        fallback={"source_type": "product", "product_kind": "simple", "confidence": 0.7, "reason": "product markers"},
    )
    variable = classifier.run(
        {
            "url": "https://example.com/product/tra-moc-cau",
            "title": "Trà Móc Câu Hảo Hạng",
            "wp_type": "product",
            "meta_description": "Trang bán trà có các quy cách.",
            "price_text": "300.000đ",
            "sku": "TRA-MC",
            "variants": [{"name": "100g"}, {"name": "250g"}, {"name": "500g"}],
            "product_markers": ["woocommerce-product", "variations_form"],
            "article_markers": [],
            "visible_excerpt": "Chọn quy cách 100g, 250g, 500g trước khi thêm vào giỏ hàng.",
        },
        fallback={"source_type": "product", "product_kind": "variable", "confidence": 0.7, "reason": "variation markers"},
    )
    assert_true(article["source_type"] == "article", f"Expected article, got {article}")
    assert_true(simple["source_type"] == "product" and simple["product_kind"] == "simple", f"Expected simple product, got {simple}")
    assert_true(variable["source_type"] == "product" and variable["product_kind"] == "variable", f"Expected variable product, got {variable}")
    return {"article": article, "simple": simple, "variable": variable}


def check_publisher_payloads() -> dict:
    base_state = {
        "url": "https://example.com/product/tra-moc-cau",
        "fetch_result": {
            "clean_content": "Trà móc câu hảo hạng có ba quy cách.",
            "metadata": {
                "author": "",
                "publish_date": "",
                "product_hints": {
                    "price_text": "300.000đ",
                    "variants": [{"name": "100g"}, {"name": "250g"}, {"name": "500g"}],
                },
                "product_kind": "variable",
            },
        },
        "extracted": {
            "steps": [],
            "product_specs": {},
            "faq_items": [{"question": "Uống khi nào?", "answer": "Phù hợp buổi sáng và đầu giờ chiều."}],
        },
        "plan": {
            "title": "Trà Móc Câu Hảo Hạng",
            "focus_keyword": "trà móc câu hảo hạng",
            "meta_title": "Trà Móc Câu Hảo Hạng",
            "meta_description": "Trà móc câu hảo hạng có nhiều quy cách dễ chọn.",
            "schema_type": "Product",
            "seo_geo_keywords": [],
            "tags": ["trà móc câu", "trà thái nguyên", "trà xanh", "trà tân cương", "trà uống hằng ngày"],
        },
        "linked_html": "<section><p>Trà móc câu hảo hạng test.</p></section>",
        "humanized": {"html": ""},
        "draft": {"html": ""},
        "image_data": {},
    }
    variable_payload = publisher._build_product_payload(base_state)
    assert_true(variable_payload["type"] == "variable", f"Expected variable payload, got {variable_payload.get('type')}")
    assert_true(len(variable_payload.get("variations", [])) == 3, "Expected 3 variations")
    assert_true(len(variable_payload.get("tags", [])) == 5, "Expected 5 product tags")
    schema = publisher.build_schema(base_state)
    assert_true(isinstance(schema, list) and len(schema) == 2, f"Expected Product + FAQ schema, got {schema}")

    simple_state = json.loads(json.dumps(base_state))
    simple_state["fetch_result"]["metadata"]["product_kind"] = "simple"
    simple_state["fetch_result"]["metadata"]["product_hints"]["variants"] = []
    simple_payload = publisher._build_product_payload(simple_state)
    assert_true(simple_payload["type"] == "simple", f"Expected simple payload, got {simple_payload.get('type')}")
    assert_true("variations" not in simple_payload, "Simple product must not include variations")
    return {
        "variable": {
            "type": variable_payload["type"],
            "attributes": variable_payload.get("attributes"),
            "variations": variable_payload.get("variations"),
            "tags": variable_payload.get("tags"),
            "schema_types": [item.get("@type") for item in schema],
        },
        "simple": {"type": simple_payload["type"]},
    }


def check_writer_sanitizer() -> dict:
    html = "<p>Đây là product variable có nhiều lựa chọn.</p><p>simple product test.</p>"
    sanitized = writer._sanitize_product_terms(html)
    assert_true("product variable" not in sanitized.lower(), "product variable leaked")
    assert_true("simple product" not in sanitized.lower(), "simple product leaked")
    return {"sanitized": sanitized}


def main() -> None:
    load_dotenv()
    result = {
        "classifier": check_classifier(),
        "publisher": check_publisher_payloads(),
        "writer": check_writer_sanitizer(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
