from __future__ import annotations

import json
import re
import unicodedata

import httpx

from app.config import get_settings


def _source_origin(state: dict) -> str:
    return str(state.get("source_origin") or "").strip().lower()


def _site_primary_color(state: dict) -> str:
    site_profile = state.get("site_profile") or {}
    color = str(site_profile.get("primary_color") or "").strip()
    if state.get("content_mode") == "per-site" and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return color
    return "#1f6f43"


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _rgba(hex_color: str, alpha: float) -> str:
    red, green, blue = _hex_to_rgb(hex_color)
    return f"rgba({red},{green},{blue},{alpha})"


def _publisher_site_config(state: dict) -> dict:
    settings = get_settings()
    site = state.get("site_profile") or {}
    config = {
        "woo_url": str(site.get("url") or "").strip(),
        "consumer_key": str(site.get("consumer_key") or "").strip(),
        "consumer_secret": str(site.get("consumer_secret") or "").strip(),
        "username": str(site.get("username") or "").strip(),
        "app_password": str(site.get("app_password") or "").strip(),
        "default_status": settings.woo_default_status,
    }
    if not config["woo_url"]:
        raise RuntimeError("Site profile is missing WooCommerce URL")
    if not (config["consumer_key"] and config["consumer_secret"]):
        raise RuntimeError("Site profile is missing WooCommerce consumer credentials")
    return config


def _faq_schema(state: dict) -> dict | None:
    faq_items = state.get("extracted", {}).get("faq_items") or []
    entities = []
    for item in faq_items:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question and answer:
            entities.append(
                {
                    "@type": "Question",
                    "name": question,
                    "acceptedAnswer": {"@type": "Answer", "text": answer},
                }
            )
    if not entities:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": entities[:8],
    }


def build_schema(state: dict) -> dict | list[dict]:
    schema_type = state["plan"]["schema_type"]
    faq_schema = _faq_schema(state)
    if schema_type == "HowTo":
        primary = {
            "@context": "https://schema.org",
            "@type": "HowTo",
            "name": state["plan"]["title"],
            "step": [{"@type": "HowToStep", "text": step} for step in state["extracted"]["steps"]],
        }
        return [primary, faq_schema] if faq_schema else primary
    if schema_type == "Product":
        primary = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": state["plan"]["title"],
            "description": state["plan"]["meta_description"],
            "image": state.get("schema_image_urls", []),
        }
        price = _extract_price_value(state)
        if price:
            primary["offers"] = {
                "@type": "Offer",
                "price": price,
                "priceCurrency": "VND",
            }
        return [primary, faq_schema] if faq_schema else primary
    primary = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": state["plan"]["title"],
        "author": {"@type": "Person", "name": state["fetch_result"]["metadata"]["author"] or "Content Forge"},
        "datePublished": state["fetch_result"]["metadata"]["publish_date"],
        "url": state["url"],
    }
    return [primary, faq_schema] if faq_schema else primary


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in ascii_value).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "product"


def _product_slug(plan: dict) -> str:
    title = (plan.get("title") or "").lower()
    focus = (plan.get("focus_keyword") or "").lower()
    source = focus or title
    words = [word for word in _slugify(source).split("-") if word and word not in {"lam", "qua", "tang", "cho", "va", "cao", "cap", "san", "pham"}]
    short = "-".join(words[:8])
    if 8 <= len(short) <= 60:
        return short
    fallback_words = [word for word in _slugify(title).split("-") if word and word not in {"lam", "qua", "tang", "cho", "va", "cao", "cap", "san", "pham"}]
    fallback = "-".join(fallback_words[:8])
    return fallback or _slugify(source)[:60]


def _extract_price_value(state: dict) -> str:
    hints = state.get("fetch_result", {}).get("metadata", {}).get("product_hints") or {}
    price_text = str(hints.get("price_text") or "").strip()
    if not price_text:
        return ""
    digits = re.sub(r"[^\d]", "", price_text)
    return digits


def _variant_attribute_name(variants: list[dict]) -> str:
    names = " ".join(str(item.get("name") or item.get("value") or "") for item in variants).lower()
    if re.search(r"\b\d+\s*g\b", names):
        return "Quy cách"
    return "Tùy chọn"


def _variant_options(state: dict) -> list[dict]:
    hints = state.get("fetch_result", {}).get("metadata", {}).get("product_hints") or {}
    specs = state.get("extracted", {}).get("product_specs") or {}
    raw_variants = hints.get("variants") or specs.get("variants") or []
    options: list[dict] = []
    seen: set[str] = set()
    for item in raw_variants:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("value") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        option = {"name": name}
        price = re.sub(r"[^\d]", "", str(item.get("price") or ""))
        if price:
            option["regular_price"] = price
        options.append(option)
        if len(options) >= 20:
            break
    return options


def _product_type_and_variations(state: dict) -> tuple[str, list[dict], list[dict]]:
    metadata = state.get("fetch_result", {}).get("metadata", {})
    product_kind = (metadata.get("product_kind") or state.get("plan", {}).get("product_kind") or "").lower()
    options = _variant_options(state)
    if product_kind != "variable":
        return "simple", [], []
    if not options:
        raise RuntimeError("Product was classified as variable, but no variation options were extracted.")
    attribute_name = _variant_attribute_name(options)
    option_names = [item["name"] for item in options]
    fallback_price = _extract_price_value(state)
    variations = []
    for option in options:
        if not option.get("regular_price") and not fallback_price:
            raise RuntimeError("Variable product variation is missing price and no fallback price is available.")
        variation = {
            "regular_price": option.get("regular_price") or fallback_price,
            "attributes": [{"name": attribute_name, "option": option["name"]}],
        }
        variations.append(variation)
    attributes = [
        {
            "name": attribute_name,
            "visible": True,
            "variation": True,
            "options": option_names,
        }
    ]
    return "variable", attributes, variations


def _extract_short_description(state: dict) -> str:
    html = state.get("linked_html") or state.get("humanized", {}).get("html") or state.get("draft", {}).get("html") or ""
    for paragraph in re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.DOTALL | re.IGNORECASE):
        stripped = re.sub(r"<[^>]+>", " ", paragraph)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if len(stripped.split()) >= 10:
            return stripped[:300]
    return state["plan"]["meta_description"]


def _product_tags(plan: dict) -> list[dict]:
    raw_tags = plan.get("tags") or plan.get("seo_geo_keywords") or []
    tags = []
    seen: set[str] = set()
    for item in raw_tags:
        name = re.sub(r"\s+", " ", str(item or "")).strip(" -–|,.;")
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append({"name": name[:42].rstrip(" -–|,.;")})
        if len(tags) >= 5:
            break
    return tags


def _style_product_content(html: str, state: dict | None = None) -> str:
    wrapper_style = "color:#333;font-size:16px;line-height:1.8;font-family:'Segoe UI',Arial,sans-serif"
    accent = _site_primary_color(state or {})
    accent_soft = _rgba(accent, 0.12)
    styled_html = re.sub(r"<blockquote[^>]*>.*?</blockquote>\s*", "", html, count=1, flags=re.DOTALL | re.IGNORECASE)
    if 'class="content-forge-product"' in styled_html:
        styled_html = re.sub(r'^<div class="content-forge-product"[^>]*>\s*', "", styled_html)
        styled_html = re.sub(r'\s*</div>\s*$', "", styled_html)
    styled_html = re.sub(r"<h2[^>]*>\s*Mô tả ngắn\s*</h2>\s*", "", styled_html, count=1, flags=re.IGNORECASE)

    replacements = {
        "<h1>": f'<h1 style="font-size:34px;line-height:1.18;margin:0 0 18px;color:{accent};letter-spacing:-.02em">',
        "<h2>": f'<h2 style="margin:40px 0 20px;font-size:24px;color:{accent};border-bottom:2px solid {accent};padding-bottom:8px;display:inline-block;line-height:1.35">',
        "<h3>": '<h3 style="margin:22px 0 12px;font-size:18px;color:#333;line-height:1.45">',
        "<p>": '<p style="margin:0 0 18px">',
        "<ul>": '<ul style="margin:0 0 30px;padding-left:22px;color:#444">',
        "<li>": '<li style="margin-bottom:12px">',
        "<table>": f'<table style="width:100%;margin:18px 0 26px;border-collapse:separate;border-spacing:0;overflow:hidden;border:1px solid {_rgba(accent, 0.22)};border-radius:10px;background:#fff">',
        "<th>": f'<th style="padding:14px 16px;border-bottom:1px solid #ecefed;vertical-align:top;background:{accent_soft};color:#333;text-align:left;font-weight:700">',
        "<td>": '<td style="padding:14px 16px;border-bottom:1px solid #ecefed;vertical-align:top">',
        "<hr />": f'<hr style="border:0;height:1px;background:linear-gradient(90deg,transparent,{_rgba(accent, 0.35)},transparent);margin:34px 0" />',
        "<figure>": '<figure style="margin:24px 0;text-align:center">',
        "<figcaption>": '<figcaption style="margin-top:12px;color:#777;font-size:14px;font-style:italic">',
    }
    for source, target in replacements.items():
        styled_html = styled_html.replace(source, target)
    styled_html = styled_html.replace("<a ", f'<a style="color:{accent};font-weight:700;text-decoration:none;border-bottom:1px solid {_rgba(accent, 0.35)}" ')
    styled_html = styled_html.replace(
        '<section class="content-forge-inline-gallery">',
        '<section class="content-forge-inline-gallery" style="margin:24px 0">'
    )
    styled_html = re.sub(
        r'<img\b([^>]*)\sstyle="[^"]*"([^>]*)>',
        r'<img\1\2>',
        styled_html,
        flags=re.IGNORECASE,
    )
    styled_html = styled_html.replace(
        '<img ',
        '<img style="width:100%;height:auto;border-radius:8px;box-shadow:0 4px 15px rgba(0,0,0,0.05);display:block" '
    )

    paragraphs = re.findall(r"<p[^>]*>.*?</p>", styled_html, flags=re.DOTALL)
    if len(paragraphs) >= 3:
        intro_1 = paragraphs[0].replace(
            '<p style="margin:0 0 18px">',
            '<p style="font-size:18px;color:#164f31;font-weight:500;font-style:italic;margin-bottom:20px">',
            1,
        )
        intro_2 = paragraphs[1].replace(
            '<p style="margin:0 0 18px">',
            '<p style="margin:0 0 24px">',
            1,
        )
        intro_3 = paragraphs[2].replace(
            '<p style="margin:0 0 18px">',
            '<p style="margin:0 0 24px;color:#444">',
            1,
        )
        styled_html = styled_html.replace(paragraphs[0], intro_1, 1)
        styled_html = styled_html.replace(paragraphs[1], intro_2, 1)
        styled_html = styled_html.replace(paragraphs[2], intro_3, 1)

    return f'<div class="content-forge-product" style="{wrapper_style}">\n{styled_html}\n</div>'


def _inject_content_images(html: str, image_urls: list[str], alt_text: str) -> str:
    if not image_urls or "content-forge-image-grid" in html or "<img " in html.lower():
        return html
    selected = image_urls[:4]
    figures = []
    for index, url in enumerate(selected, start=1):
        figures.append(
            f'<figure><img src="{url}" alt="{alt_text}" loading="lazy" '
            'style="width:100%;height:auto;border-radius:14px;display:block" />'
            f'<figcaption>Hình ảnh tham chiếu sản phẩm #{index}</figcaption></figure>'
        )
    image_block = (
        '<section class="content-forge-image-grid">'
        + "".join(figures)
        + "</section>"
    )
    # Insert after the first </p> that follows the first <h2>
    import re as _re
    match = _re.search(r'(<h2[^>]*>.*?</h2>\s*(?:<p[^>]*>.*?</p>))', html, _re.DOTALL)
    if match:
        insert_pos = match.end()
        return html[:insert_pos] + "\n" + image_block + html[insert_pos:]
    return image_block + "\n" + html


def _seo_title(state: dict) -> str:
    plan = state["plan"]
    focus_keyword = plan["focus_keyword"]
    title = (plan.get("meta_title") or plan.get("title") or focus_keyword).strip()
    if not title:
        title = focus_keyword
    return title[:60].rstrip(" -|:,;")


def _seo_description(plan: dict) -> str:
    focus_keyword = plan["focus_keyword"]
    description = plan.get("meta_description") or ""
    if focus_keyword.lower() not in description.lower():
        description = f"{focus_keyword} có thông tin rõ ràng, hình ảnh thực tế và cách dùng phù hợp với nhu cầu mua hàng hiện tại."
    return description[:155]


def _build_product_payload(state: dict) -> dict:
    settings = get_settings()
    status = state.get("publish_status") or settings.woo_default_status
    schema = build_schema(state)
    category_ids = [state["woo_category_id"]] if state.get("woo_category_id") else []
    image_data = state.get("image_data", {}) or {}
    uploaded = image_data.get("uploaded") or []
    image_gallery = image_data.get("gallery") or []
    uploaded_ids = [int(item["id"]) for item in uploaded if item.get("id")]
    price_value = _extract_price_value(state)
    product_type, attributes, variations = _product_type_and_variations(state)
    meta_title = _seo_title(state)
    meta_description = _seo_description(state["plan"])
    images = (
        [{"id": int(item["id"]), "alt": item.get("alt", "")} for item in uploaded[:8] if item.get("id")]
        or [{"src": url, "alt": image_data.get("alt_text", "")} for url in image_gallery[:8]]
    )
    payload = {
        "name": state["plan"]["title"],
        "slug": _product_slug(state["plan"]),
        "type": product_type,
        "status": status,
        "description": _style_product_content(
            _inject_content_images(
                state["linked_html"],
                image_gallery,
                image_data.get("alt_text", state["plan"]["focus_keyword"]),
            ),
            state,
        ),
        "short_description": _extract_short_description(state),
        "image_url": image_data.get("url", ""),
        "image_alt": image_data.get("alt_text", ""),
        "featured_image_id": uploaded_ids[0] if uploaded_ids else None,
        "gallery_image_ids": uploaded_ids,
        "image_gallery": image_gallery,
        "images": images,
        "categories": [{"id": cid} for cid in category_ids],
        "category_ids": category_ids,
        "tags": _product_tags(state["plan"]),
        "meta_data": [
            {"key": "rank_math_title", "value": meta_title},
            {"key": "rank_math_description", "value": meta_description},
            {"key": "rank_math_focus_keyword", "value": state["plan"]["focus_keyword"]},
            {"key": "rank_math_robots", "value": ["index", "follow"]},
            {"key": "_content_forge_schema", "value": json.dumps(schema, ensure_ascii=False)},
        ],
        "meta": {
            "rank_math_title": meta_title,
            "rank_math_description": meta_description,
            "rank_math_focus_keyword": state["plan"]["focus_keyword"],
            "rank_math_robots": ["index", "follow"],
            "_content_forge_schema": json.dumps(schema, ensure_ascii=False),
        },
    }
    if attributes:
        payload["attributes"] = attributes
    if variations:
        payload["variations"] = variations
    if price_value and product_type == "simple":
        payload["regular_price"] = price_value
    return payload


def _build_shopee_product_payload(state: dict) -> dict:
    settings = get_settings()
    status = state.get("publish_status") or settings.woo_default_status
    schema = build_schema(state)
    category_ids = [state["woo_category_id"]] if state.get("woo_category_id") else []
    image_data = state.get("image_data", {}) or {}
    uploaded = image_data.get("uploaded") or []
    image_gallery = image_data.get("gallery") or []
    uploaded_ids = [int(item["id"]) for item in uploaded if item.get("id")]
    normalized = ((state.get("source_seed") or {}).get("normalized") or {})

    normalized_type = str(normalized.get("type") or "simple").strip().lower()
    normalized_attributes = normalized.get("attributes") or []
    normalized_variations = normalized.get("variations") or []

    product_type = "variable" if normalized_type == "variable" and normalized_variations else "simple"
    attributes = []
    variations = []
    if product_type == "variable":
        for attribute in normalized_attributes:
            if not isinstance(attribute, dict):
                continue
            name = str(attribute.get("name") or "").strip()
            options = [str(item).strip() for item in (attribute.get("options") or []) if str(item).strip()]
            if not name or not options:
                continue
            attributes.append(
                {
                    "name": name,
                    "visible": bool(attribute.get("visible", True)),
                    "variation": bool(attribute.get("variation", False)),
                    "options": options,
                }
            )
        for variation in normalized_variations:
            if not isinstance(variation, dict):
                continue
            variation_attributes = []
            for attr_name, option in (variation.get("attributes") or {}).items():
                attr_name = str(attr_name or "").strip()
                option = str(option or "").strip()
                if attr_name and option:
                    variation_attributes.append({"name": attr_name, "option": option})
            if not variation_attributes:
                continue
            regular_price = re.sub(r"[^\d]", "", str(variation.get("regular_price") or variation.get("sale_price") or ""))
            if not regular_price:
                continue
            variations.append(
                {
                    "regular_price": regular_price,
                    "attributes": variation_attributes,
                }
            )
        if not attributes or not variations:
            raise RuntimeError("Shopee variable product is missing normalized attributes or variations.")

    meta_title = _seo_title(state)
    meta_description = _seo_description(state["plan"])
    images = (
        [{"id": int(item["id"]), "alt": item.get("alt", "")} for item in uploaded[:8] if item.get("id")]
        or [{"src": url, "alt": image_data.get("alt_text", "")} for url in image_gallery[:8]]
    )
    payload = {
        "name": state["plan"]["title"],
        "slug": _product_slug(state["plan"]),
        "type": product_type,
        "status": status,
        "description": _style_product_content(
            _inject_content_images(
                state["linked_html"],
                image_gallery,
                image_data.get("alt_text", state["plan"]["focus_keyword"]),
            ),
            state,
        ),
        "short_description": _extract_short_description(state),
        "image_url": image_data.get("url", ""),
        "image_alt": image_data.get("alt_text", ""),
        "featured_image_id": uploaded_ids[0] if uploaded_ids else None,
        "gallery_image_ids": uploaded_ids,
        "image_gallery": image_gallery,
        "images": images,
        "categories": [{"id": cid} for cid in category_ids],
        "category_ids": category_ids,
        "tags": _product_tags(state["plan"]),
        "meta_data": [
            {"key": "rank_math_title", "value": meta_title},
            {"key": "rank_math_description", "value": meta_description},
            {"key": "rank_math_focus_keyword", "value": state["plan"]["focus_keyword"]},
            {"key": "rank_math_robots", "value": ["index", "follow"]},
            {"key": "_content_forge_schema", "value": json.dumps(schema, ensure_ascii=False)},
        ],
        "meta": {
            "rank_math_title": meta_title,
            "rank_math_description": meta_description,
            "rank_math_focus_keyword": state["plan"]["focus_keyword"],
            "rank_math_robots": ["index", "follow"],
            "_content_forge_schema": json.dumps(schema, ensure_ascii=False),
        },
    }
    if attributes:
        payload["attributes"] = attributes
    if variations:
        payload["variations"] = variations

    if product_type == "simple":
        simple_price = re.sub(r"[^\d]", "", str(normalized.get("regular_price") or normalized.get("sale_price") or _extract_price_value(state) or ""))
        if simple_price:
            payload["regular_price"] = simple_price
    return payload


def _publish_via_rest(state: dict, payload: dict) -> dict:
    site_config = _publisher_site_config(state)
    if not site_config["woo_url"]:
        raise RuntimeError("WooCommerce URL is missing")

    if not (site_config["consumer_key"] and site_config["consumer_secret"]):
        raise RuntimeError("WooCommerce credentials are incomplete")

    base = site_config["woo_url"].rstrip("/")
    candidates = [
        f"{base}/wp-json/wc/v3/products",
        f"{base}/index.php?rest_route=/wc/v3/products",
    ]
    params = {
        "consumer_key": site_config["consumer_key"],
        "consumer_secret": site_config["consumer_secret"],
    }

    last_error: Exception | None = None
    for url in candidates:
        try:
            response = httpx.post(url, params=params, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()
            _create_variations_via_rest(base, int(data["id"]), payload, params=params, auth=None)
            return {
                "woo_post_id": data["id"],
                "woo_link": data.get("permalink") or data.get("link") or data.get("slug", ""),
            }
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"WooCommerce REST publish failed: {last_error}")


def _create_variations_via_rest(
    base: str,
    product_id: int,
    payload: dict,
    params: dict | None = None,
    auth: tuple[str, str] | None = None,
    local_index_route: bool = False,
) -> None:
    if payload.get("type") != "variable" or not payload.get("variations"):
        return
    params = params or {}
    if local_index_route:
        url = f"{base.rstrip('/')}/index.php"
        request_params = {"rest_route": f"/wc/v3/products/{product_id}/variations", **params}
    else:
        url = f"{base.rstrip('/')}/wp-json/wc/v3/products/{product_id}/variations"
        request_params = params
    for variation in payload.get("variations", []):
        response = httpx.post(
            url,
            params=request_params,
            auth=auth,
            json=variation,
            timeout=60,
        )
        response.raise_for_status()


def run(state: dict) -> dict:
    schema = build_schema(state)
    payload = _build_product_payload(state)
    publish_result = _publish_via_rest(state, payload)

    return {
        "woo_post_id": publish_result["woo_post_id"],
        "woo_link": publish_result["woo_link"],
        "final_article": {
            "title": state["plan"]["title"],
            "html": state["linked_html"],
            "schema": schema,
        },
    }


def run_shopee(state: dict) -> dict:
    if _source_origin(state) != "shopee":
        return run(state)

    schema = build_schema(state)
    payload = _build_shopee_product_payload(state)
    publish_result = _publish_via_rest(state, payload)

    return {
        "woo_post_id": publish_result["woo_post_id"],
        "woo_link": publish_result["woo_link"],
        "final_article": {
            "title": state["plan"]["title"],
            "html": state["linked_html"],
            "schema": schema,
        },
    }
