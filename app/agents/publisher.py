from __future__ import annotations

import json
import os
import tempfile
import re
import unicodedata
from html import escape, unescape
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

from app.config import get_settings
from app.site_store import get_site


IMAGE_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


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
    site = dict(state.get("site_profile") or {})
    site_id = str(state.get("site_id") or site.get("site_id") or "").strip()
    if site_id:
        latest_site = get_site(site_id)
        if latest_site:
            site = {**site, **latest_site}
    config = {
        "woo_url": str(site.get("url") or "").strip(),
        "consumer_key": str(site.get("consumer_key") or "").strip(),
        "consumer_secret": str(site.get("consumer_secret") or "").strip(),
        "username": str(site.get("username") or "").strip(),
        "app_password": str(site.get("app_password") or "").strip(),
        "default_status": settings.woo_default_status,
        "shopee_affiliate_post_type": str(
            site.get("shopee_affiliate_post_type")
            or site.get("affiliate_post_type")
            or os.getenv("SHOPEE_AFFILIATE_POST_TYPE")
            or "affiliate_product"
        ).strip(),
        "shopee_affiliate_rest_base": str(
            site.get("shopee_affiliate_rest_base")
            or site.get("affiliate_rest_base")
            or os.getenv("SHOPEE_AFFILIATE_REST_BASE")
            or ""
        ).strip(),
        "shopee_affiliate_query": str(
            site.get("shopee_affiliate_query")
            or site.get("affiliate_query")
            or os.getenv("SHOPEE_AFFILIATE_QUERY")
            or ""
        ).strip(),
        "shopee_affiliate_params": site.get("shopee_affiliate_params") or site.get("affiliate_params") or {},
    }
    if not config["woo_url"]:
        raise RuntimeError("Site profile is missing WooCommerce URL")
    return config


def _image_extension(content_type: str, content: bytes, url: str) -> str:
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type in IMAGE_CONTENT_TYPES:
        return IMAGE_CONTENT_TYPES[normalized_type]
    lowered_url = url.lower().split("?", 1)[0]
    for extension in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        if lowered_url.endswith(extension):
            return ".jpg" if extension == ".jpeg" else extension
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return ".gif"
    return ""


def _normalize_image_url(url: str) -> str:
    value = unescape(str(url or "")).strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = f"https:{value}"
    elif re.match(r"^[a-z0-9.-]+\.[a-z]{2,}/", value, flags=re.IGNORECASE):
        value = f"https://{value}"
    if "*" in value or "%2a" in value.lower() or "*" in unquote(value):
        return ""
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        return ""
    return value


def _media_source_url(data: dict) -> str:
    media_details = data.get("media_details") or {}
    sizes = media_details.get("sizes") or {}
    full = sizes.get("full") or {}
    for value in [
        full.get("source_url"),
        data.get("source_url"),
        (data.get("guid") or {}).get("rendered"),
    ]:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _url_reachable(client: httpx.Client, url: str) -> bool:
    if not url:
        return False
    try:
        response = client.head(url, timeout=15)
        if response.status_code == 405:
            response = client.get(url, headers={"Range": "bytes=0-0"}, timeout=15)
        return 200 <= response.status_code < 400
    except Exception:
        return False


def _upload_wp_media_from_temp(site_config: dict, image_url: str, alt_text: str, index: int) -> dict | None:
    if not (site_config.get("username") and site_config.get("app_password")):
        return None
    image_url = _normalize_image_url(image_url)
    if not image_url:
        return None
    try:
        with httpx.Client(follow_redirects=True, timeout=45) as client:
            download = client.get(
                image_url,
                headers={
                    "User-Agent": "Mozilla/5.0 ContentForge/1.0",
                    "Accept": "image/webp,image/jpeg,image/png,image/gif,image/*;q=0.8,*/*;q=0.5",
                    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
                    "Referer": "https://shopee.vn/",
                },
            )
            download.raise_for_status()
            content = download.content
            if not content or len(content) > 12_000_000:
                return None
            extension = _image_extension(download.headers.get("content-type", ""), content, image_url)
            if not extension:
                return None
            filename = f"content-forge-shopee-{index}{extension}"
            with tempfile.NamedTemporaryFile(prefix="content-forge-shopee-", suffix=extension, delete=False) as tmp_file:
                tmp_file.write(content)
                tmp_path = Path(tmp_file.name)
            try:
                media_url = f"{site_config['woo_url'].rstrip('/')}/wp-json/wp/v2/media"
                with tmp_path.open("rb") as file_obj:
                    upload = client.post(
                        media_url,
                        auth=(site_config["username"], site_config["app_password"]),
                        headers={
                            "Content-Disposition": f'attachment; filename="{filename}"',
                            "Content-Type": download.headers.get("content-type", "application/octet-stream").split(";", 1)[0],
                        },
                        content=file_obj.read(),
                    )
                upload.raise_for_status()
                data = upload.json()
                media_id = data.get("id")
                if not media_id:
                    return None
                uploaded_url = _media_source_url(data)
                if uploaded_url and not _url_reachable(client, uploaded_url):
                    uploaded_url = ""
                media_item = {
                    "id": int(media_id),
                    "alt": alt_text,
                    "url": uploaded_url,
                    "source_url": image_url,
                }
                try:
                    client.post(
                        f"{media_url}/{media_id}",
                        auth=(site_config["username"], site_config["app_password"]),
                        json={"alt_text": alt_text},
                    )
                except Exception:
                    pass
                return media_item
            finally:
                tmp_path.unlink(missing_ok=True)
    except Exception:
        return None


def _upload_shopee_images_if_needed(state: dict, payload: dict) -> None:
    if payload.get("images"):
        return
    image_data = state.get("image_data", {}) or {}
    image_urls = [str(url).strip() for url in (image_data.get("gallery") or []) if str(url).strip()]
    if not image_urls:
        return
    site_config = _publisher_site_config(state)
    alt_text = str(image_data.get("alt_text") or state.get("plan", {}).get("focus_keyword") or state.get("plan", {}).get("title") or "Shopee product")
    uploaded = []
    for index, image_url in enumerate(image_urls[:6], start=1):
        media_item = _upload_wp_media_from_temp(site_config, image_url, alt_text, index)
        if media_item:
            uploaded.append(media_item)
    if uploaded:
        payload["images"] = uploaded
        payload["featured_image_id"] = uploaded[0]["id"]
        payload["gallery_image_ids"] = [item["id"] for item in uploaded]


def _upload_shopee_affiliate_images(state: dict) -> list[dict]:
    # Affiliate posts should hotlink Shopee CDN images instead of creating WP
    # media attachments. This avoids filling the target site's disk.
    return []


def _wp_rest_collection_base(value: str) -> str:
    rest_base = re.sub(r"^/+", "", str(value or "").strip())
    rest_base = re.sub(r"/+$", "", rest_base)
    if rest_base in {"", "post"}:
        return "posts"
    return rest_base


def _affiliate_query_items(site_config: dict) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    params = site_config.get("shopee_affiliate_params") or {}
    if isinstance(params, dict):
        for key, value in params.items():
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if key_text and value_text:
                items.append((key_text, value_text))
    query = str(site_config.get("shopee_affiliate_query") or "").strip().lstrip("?")
    if query:
        for key, value in parse_qsl(query, keep_blank_values=False):
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if key_text and value_text:
                items.append((key_text, value_text))
    return items


def _shopee_domain(source_url: str) -> str:
    host = urlsplit(source_url).netloc.lower()
    if host and "shopee." in host:
        return host
    return "shopee.vn"


def _clean_shopee_product_url(source_url: str, normalized: dict) -> str:
    item_id = str(normalized.get("item_id") or "").strip()
    shop_id = str(normalized.get("shop_id") or "").strip()
    if item_id and shop_id:
        return f"https://{_shopee_domain(source_url)}/product/{shop_id}/{item_id}"

    parts = urlsplit(source_url)
    if not parts.scheme or not parts.netloc:
        return source_url.strip()
    path = re.sub(r"/+", "/", parts.path or "/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _shopee_affiliate_url(state: dict) -> str:
    site_config = _publisher_site_config(state)
    normalized = ((state.get("source_seed") or {}).get("normalized") or {})
    source_url = str(normalized.get("source_url") or state.get("url") or "").strip()
    clean_url = _clean_shopee_product_url(source_url, normalized)
    configured_items = _affiliate_query_items(site_config)
    if not configured_items:
        return clean_url

    parts = urlsplit(clean_url)
    query_items = parse_qsl(parts.query, keep_blank_values=False)
    existing_keys = {key for key, _ in query_items}
    for key, value in configured_items:
        if key not in existing_keys:
            query_items.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items, doseq=True), ""))


def _shopee_source_image_urls(state: dict) -> list[str]:
    image_data = state.get("image_data", {}) or {}
    source_seed = state.get("source_seed") or {}
    normalized = source_seed.get("normalized") or {}
    candidates = list(image_data.get("gallery") or []) + list(normalized.get("images") or [])
    output: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = _normalize_image_url(str(candidate).strip())
        if url and url not in seen:
            seen.add(url)
            output.append(url)
    return output


def _contains_redacted_marker(value) -> bool:
    text = str(value or "")
    return "*" in text or "%2a" in text.lower() or "*" in unquote(text)


def _strip_redacted_images_from_html(html: str) -> str:
    def replace_figure(match: re.Match[str]) -> str:
        return "" if _contains_redacted_marker(match.group(0)) else match.group(0)

    def replace_img(match: re.Match[str]) -> str:
        return "" if _contains_redacted_marker(match.group(0)) else match.group(0)

    cleaned = re.sub(r"<figure\b[^>]*>.*?</figure>\s*", replace_figure, html or "", flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<img\b[^>]*>\s*", replace_img, cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"https?://[^\s\"'<>]*\*[^\s\"'<>]*", "", cleaned)
    return cleaned


def _shopee_affiliate_content(state: dict, uploaded_images: list[dict]) -> str:
    normalized = ((state.get("source_seed") or {}).get("normalized") or {})
    image_data = state.get("image_data", {}) or {}
    gallery_urls = _shopee_source_image_urls(state)
    alt_text = str(image_data.get("alt_text") or state["plan"]["focus_keyword"])
    source_url = _shopee_affiliate_url(state)
    price = re.sub(r"\s+", " ", str(normalized.get("sale_price") or normalized.get("regular_price") or "")).strip()

    content = _style_product_content(
        _inject_content_images(state["linked_html"], gallery_urls, alt_text, force=True),
        state,
    )

    price_html = f"<p><strong>Giá tham khảo:</strong> {price}</p>" if price else ""
    button_html = ""
    if source_url:
        button_html = (
            '<p><a href="' + escape(source_url, quote=True) + '" rel="nofollow sponsored" target="_blank">'
            "Xem sản phẩm trên Shopee"
            "</a></p>"
        )
    gallery_html = ""
    if gallery_urls:
        figures = []
        for index, image_url in enumerate(gallery_urls[:8], start=1):
            figures.append(
                '<figure><img src="'
                + escape(image_url, quote=True)
                + '" alt="'
                + escape(f"{alt_text} {index}", quote=True)
                + '" loading="lazy" /></figure>'
            )
        gallery_html = '<section class="content-forge-affiliate-gallery"><h2>Hình ảnh sản phẩm</h2>' + "".join(figures) + "</section>"

    affiliate_block = (
        '<section class="content-forge-affiliate-box">'
        "<h2>Thông tin mua hàng</h2>"
        f"{price_html}"
        "<p>Sản phẩm được giới thiệu theo mô hình affiliate. Giá, tồn kho và ưu đãi có thể thay đổi theo thời điểm trên sàn.</p>"
        f"{button_html}"
        "</section>"
        f"{gallery_html}"
    )
    if "</div>" in content:
        return content.rsplit("</div>", 1)[0] + affiliate_block + "\n</div>"
    return content + affiliate_block


def _sanitize_shopee_affiliate_payload(payload: dict, state: dict, uploaded_images: list[dict]) -> dict:
    source_urls = _shopee_source_image_urls(state)
    if not source_urls:
        raise RuntimeError("Shopee affiliate publish blocked: no clean image URL is available after filtering redacted URLs")

    payload["content"] = _strip_redacted_images_from_html(str(payload.get("content") or ""))
    if _contains_redacted_marker(payload.get("content")):
        payload["content"] = _shopee_affiliate_content(state, uploaded_images)

    meta = payload.get("meta")
    if isinstance(meta, dict):
        meta["gallery_image_urls"] = source_urls
        meta["_gallery_image_urls"] = source_urls
        meta["product_gallery_urls"] = source_urls
        meta["_product_gallery_urls"] = source_urls

    if _contains_redacted_marker(payload.get("content")) or _contains_redacted_marker(meta):
        raise RuntimeError("Shopee affiliate publish blocked: redacted image URL remains in final WordPress payload")
    return payload


def _build_shopee_affiliate_payload(state: dict) -> tuple[dict, str, str]:
    settings = get_settings()
    site_config = _publisher_site_config(state)
    normalized = ((state.get("source_seed") or {}).get("normalized") or {})
    uploaded_images = _upload_shopee_affiliate_images(state)
    schema = build_schema(state)
    status = state.get("publish_status") or settings.woo_default_status
    post_type = site_config.get("shopee_affiliate_post_type") or "affiliate_product"
    rest_base = site_config.get("shopee_affiliate_rest_base") or post_type
    meta_title = _seo_title(state)
    meta_description = _seo_description(state["plan"])
    original_source_url = str(normalized.get("source_url") or state.get("url") or "").strip()
    clean_product_url = _clean_shopee_product_url(original_source_url, normalized)
    affiliate_url = _shopee_affiliate_url(state)
    regular_price = re.sub(r"[^\d]", "", str(normalized.get("regular_price") or ""))
    sale_price = re.sub(r"[^\d]", "", str(normalized.get("sale_price") or ""))
    source_urls = _shopee_source_image_urls(state)
    gallery_urls = source_urls

    payload = {
        "title": state["plan"]["title"],
        "content": _shopee_affiliate_content(state, uploaded_images),
        "status": status,
        "slug": _product_slug(state["plan"]),
        "excerpt": _extract_short_description(state),
        "meta": {
            "rank_math_title": meta_title,
            "rank_math_description": meta_description,
            "rank_math_focus_keyword": state["plan"]["focus_keyword"],
            "rank_math_robots": ["index", "follow"],
            "_content_forge_schema": json.dumps(schema, ensure_ascii=False),
            "_content_forge_source_origin": "shopee",
            "_content_forge_source_url": affiliate_url,
            "_content_forge_original_source_url": original_source_url,
            "_content_forge_clean_product_url": clean_product_url,
            "_content_forge_shopee_item_id": str(normalized.get("item_id") or ""),
            "_content_forge_shopee_shop_id": str(normalized.get("shop_id") or ""),
            "affiliate_url": affiliate_url,
            "_affiliate_url": affiliate_url,
            "product_url": affiliate_url,
            "_product_url": affiliate_url,
            "shopee_url": affiliate_url,
            "clean_product_url": clean_product_url,
            "regular_price": regular_price,
            "sale_price": sale_price,
            "price": sale_price or regular_price,
            "gallery_image_ids": [],
            "_gallery_image_ids": [],
            "gallery_image_urls": gallery_urls,
            "_gallery_image_urls": gallery_urls,
            "product_gallery": [],
            "_product_gallery": [],
            "product_gallery_images": [],
            "_product_gallery_images": [],
            "product_image_gallery": "",
            "_product_image_gallery": "",
            "image_gallery": "",
            "_image_gallery": "",
            "product_gallery_urls": gallery_urls,
            "_product_gallery_urls": gallery_urls,
        },
    }
    payload = _sanitize_shopee_affiliate_payload(payload, state, uploaded_images)
    return payload, post_type, _wp_rest_collection_base(rest_base)


def _publish_wp_post_type_via_rest(state: dict, payload: dict, post_type: str, rest_base: str) -> dict:
    site_config = _publisher_site_config(state)
    if not (site_config.get("username") and site_config.get("app_password")):
        raise RuntimeError("WordPress affiliate publish requires site username and application password")

    base = site_config["woo_url"].rstrip("/")
    rest_base = _wp_rest_collection_base(rest_base)
    candidates = [
        (f"{base}/wp-json/wp/v2/{rest_base}", None, False),
        (f"{base}/index.php", {"rest_route": f"/wp/v2/{rest_base}"}, True),
    ]
    auth = (site_config["username"], site_config["app_password"])
    payload_variants = [
        payload,
        {key: value for key, value in payload.items() if key != "meta"},
        {key: value for key, value in payload.items() if key in {"title", "content", "status", "slug", "featured_media"}},
    ]

    errors: list[str] = []
    for url, params, local_index_route in candidates:
        for variant_index, variant in enumerate(payload_variants, start=1):
            try:
                response = httpx.post(url, params=params, auth=auth, json=variant, timeout=60)
                response.raise_for_status()
                data = response.json()
                return {
                    "woo_post_id": data["id"],
                    "woo_link": data.get("link") or data.get("permalink") or "",
                    "published_post_type": post_type,
                    "published_rest_base": rest_base,
                }
            except httpx.HTTPStatusError as exc:
                response = exc.response
                body = re.sub(r"\s+", " ", response.text or "").strip()[:500]
                route = "index_rest_route" if local_index_route else "wp_json"
                errors.append(f"{route} POST {response.status_code} variant {variant_index}: {body}")
            except Exception as exc:
                route = "index_rest_route" if local_index_route else "wp_json"
                errors.append(f"{route} POST error variant {variant_index}: {exc}")
    raise RuntimeError(
        f"WordPress affiliate publish failed for post_type={post_type!r}, rest_base={rest_base!r}: "
        + " | ".join(errors)
    )


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
            continue
        variation = {
            "regular_price": option.get("regular_price") or fallback_price,
            "attributes": [{"name": attribute_name, "option": option["name"]}],
        }
        variations.append(variation)
    if not variations:
        return "simple", [], []
    attributes = [
        {
            "name": attribute_name,
            "visible": True,
            "variation": True,
            "options": [item["attributes"][0]["option"] for item in variations],
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


def _inject_content_images(html: str, image_urls: list[str], alt_text: str, force: bool = False) -> str:
    html = _remove_all_content_images(html or "") if force else _remove_invalid_content_images(html or "")
    if not image_urls:
        return html
    if not force and _has_valid_content_image(html):
        return html
    html = re.sub(r"<section\b[^>]*content-forge-image-grid[^>]*>\s*</section>\s*", "", html, flags=re.IGNORECASE | re.DOTALL)
    if "<img " in html.lower():
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


def _has_valid_content_image(html: str) -> bool:
    for match in re.finditer(r"<img\b[^>]*>", html or "", flags=re.IGNORECASE | re.DOTALL):
        src_match = re.search(r"""\bsrc\s*=\s*(['"])(.*?)\1""", match.group(0), flags=re.IGNORECASE | re.DOTALL)
        if src_match and _normalize_image_url(src_match.group(2).strip()):
            return True
    return False


def _remove_all_content_images(html: str) -> str:
    cleaned = re.sub(r"<figure\b[^>]*>.*?<img\b.*?</figure>\s*", "", html or "", flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<img\b[^>]*>\s*", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<section\b[^>]*(?:content-forge-image-grid|content-forge-inline-gallery|content-forge-affiliate-gallery)[^>]*>.*?</section>\s*", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned


def _remove_invalid_content_images(html: str) -> str:
    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        src_match = re.search(r"""\bsrc\s*=\s*(['"])(.*?)\1""", tag, flags=re.IGNORECASE | re.DOTALL)
        if not src_match:
            return ""
        src = src_match.group(2).strip()
        if not _normalize_image_url(src):
            return ""
        return tag

    cleaned = re.sub(r"<img\b[^>]*>", replace, html or "", flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<figure[^>]*>\s*(?:<figcaption[^>]*>.*?</figcaption>\s*)?</figure>\s*", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned


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
        fallback_price = re.sub(r"[^\d]", "", str(normalized.get("regular_price") or normalized.get("sale_price") or ""))
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
            regular_price = re.sub(r"[^\d]", "", str(variation.get("regular_price") or variation.get("sale_price") or fallback_price))
            if not regular_price:
                continue
            variations.append(
                {
                    "regular_price": regular_price,
                    "attributes": variation_attributes,
                }
            )
        if not attributes or not variations:
            product_type = "simple"
            attributes = []
            variations = []

    meta_title = _seo_title(state)
    meta_description = _seo_description(state["plan"])
    # Shopee CDN URLs often have formats/headers WooCommerce rejects when it
    # tries to sideload them. Only send already-uploaded WP media IDs here.
    images = [{"id": int(item["id"]), "alt": item.get("alt", "")} for item in uploaded[:8] if item.get("id")]
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
        (f"{base}/wp-json/wc/v3/products", None, False),
        (f"{base}/index.php", {"rest_route": "/wc/v3/products"}, True),
    ]
    auth = (site_config["consumer_key"], site_config["consumer_secret"])

    errors: list[str] = []
    for url, params, local_index_route in candidates:
        try:
            response = httpx.post(url, params=params, auth=auth, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()
            _create_variations_via_rest(
                base,
                int(data["id"]),
                payload,
                params={},
                auth=auth,
                local_index_route=local_index_route,
            )
            return {
                "woo_post_id": data["id"],
                "woo_link": data.get("permalink") or data.get("link") or data.get("slug", ""),
            }
        except httpx.HTTPStatusError as exc:
            response = exc.response
            body = re.sub(r"\s+", " ", response.text or "").strip()[:500]
            route = "index_rest_route" if local_index_route else "wp_json"
            errors.append(f"{route} POST {response.status_code}: {body}")
        except Exception as exc:
            route = "index_rest_route" if local_index_route else "wp_json"
            errors.append(f"{route} POST error: {exc}")
    raise RuntimeError("WooCommerce REST publish failed: " + " | ".join(errors))


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
    payload, post_type, rest_base = _build_shopee_affiliate_payload(state)
    publish_result = _publish_wp_post_type_via_rest(state, payload, post_type, rest_base)

    return {
        "woo_post_id": publish_result["woo_post_id"],
        "woo_link": publish_result["woo_link"],
        "published_post_type": publish_result.get("published_post_type"),
        "published_rest_base": publish_result.get("published_rest_base"),
        "final_article": {
            "title": state["plan"]["title"],
            "html": state["linked_html"],
            "schema": schema,
        },
    }
