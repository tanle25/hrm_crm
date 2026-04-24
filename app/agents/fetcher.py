from __future__ import annotations

import html
import json
import re
import subprocess
from urllib.parse import urlparse, urlunparse

from app.agents import classifier

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None


def _title_from_url(url: str) -> str:
    parsed = urlparse(url)
    slug = parsed.path.strip("/").replace("-", " ") or parsed.netloc
    return slug.title()


def _extract_wordpress_api_url(html: str) -> str | None:
    match = re.search(r'<link[^>]+rel="https://api\.w\.org/"[^>]+href="([^"]+)"', html, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _canonical_fetch_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _curl_get(url: str) -> str | None:
    try:
        result = subprocess.run(
            ["curl", "-sSL", "--max-time", "30", url],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except Exception:
        return None


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _extract_meta(page_html: str, key: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+name=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(key)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE)
        if match:
            return html.unescape(_strip_html(match.group(1)))
    return ""


def _extract_html_title(page_html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html.unescape(_strip_html(match.group(1)))


def _extract_json_ld_blocks(page_html: str) -> list[dict]:
    blocks: list[dict] = []
    for match in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page_html, re.IGNORECASE | re.DOTALL):
        raw = html.unescape(match.group(1)).strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, dict):
            blocks.append(parsed)
    return blocks


def _find_product_schema(page_html: str) -> dict:
    for block in _extract_json_ld_blocks(page_html):
        graph = block.get("@graph")
        candidates = graph if isinstance(graph, list) else [block]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return {}


def _schema_type(value: object) -> set[str]:
    if isinstance(value, str):
        return {value.lower()}
    if isinstance(value, list):
        return {str(item).lower() for item in value}
    return set()


def _find_article_schema(page_html: str) -> dict:
    article_types = {"article", "blogposting", "newsarticle", "creativework"}
    for block in _extract_json_ld_blocks(page_html):
        graph = block.get("@graph")
        candidates = graph if isinstance(graph, list) else [block]
        for item in candidates:
            if isinstance(item, dict) and (_schema_type(item.get("@type")) & article_types):
                return item
    return {}


def _detect_product_kind(page_html: str, product_schema: dict) -> str:
    lower = page_html.lower()
    offers = product_schema.get("offers") if isinstance(product_schema, dict) else {}
    if isinstance(offers, dict):
        if str(offers.get("@type") or "").lower() == "aggregateoffer":
            return "variable"
        if offers.get("lowPrice") and offers.get("highPrice") and str(offers.get("lowPrice")) != str(offers.get("highPrice")):
            return "variable"
        if offers.get("offerCount") and str(offers.get("offerCount")) not in {"", "1"}:
            return "variable"
    if isinstance(offers, list) and len(offers) > 1:
        return "variable"
    variation_markers = [
        "variations_form",
        "woocommerce-variation",
        "data-product_variations",
        "variable-item",
        "single_variation_wrap",
        "variation_id",
    ]
    if any(marker in lower for marker in variation_markers):
        return "variable"
    return "simple"


def _detect_source_classification(page_html: str, url: str, wp_type: str | None = None) -> dict:
    product_schema = _find_product_schema(page_html)
    article_schema = _find_article_schema(page_html)
    lower = page_html.lower()
    wp_type_lower = (wp_type or "").lower()
    is_product = bool(product_schema) or wp_type_lower == "product" or "woocommerce-product" in lower
    is_article = bool(article_schema) or wp_type_lower in {"post", "page"} or not is_product
    source_type = "product" if is_product else "article"
    return {
        "source_type": source_type,
        "source_kind": source_type,
        "product_kind": _detect_product_kind(page_html, product_schema) if is_product else "",
        "confidence": 0.65 if is_product or article_schema else 0.35,
        "reason": "heuristic evidence from schema, WordPress type and page markers",
        "has_product_schema": bool(product_schema),
        "has_article_schema": bool(article_schema),
        "wp_type": wp_type or "",
        "canonical_url": _canonical_fetch_url(url),
    }


def _classification_evidence(page_html: str, url: str, wp_type: str | None, title: str, hints: dict, heuristic: dict) -> dict:
    visible = _strip_html(page_html)
    product_markers = [
        marker
        for marker in [
            "woocommerce-product",
            "add_to_cart",
            "variations_form",
            "data-product_variations",
            "sku",
            "giỏ hàng",
            "chọn một tùy chọn",
        ]
        if marker.lower() in page_html.lower()
    ]
    article_markers = [
        marker
        for marker in ["article", "blogposting", "entry-content", "post-title", "published_time"]
        if marker.lower() in page_html.lower()
    ]
    return {
        "url": url,
        "title": title,
        "wp_type": wp_type or "",
        "heuristic": heuristic,
        "meta_description": hints.get("meta_description", ""),
        "price_text": hints.get("price_text", ""),
        "sku": hints.get("sku", ""),
        "category": hints.get("category", ""),
        "variants": hints.get("variants", [])[:8],
        "product_markers": product_markers,
        "article_markers": article_markers,
        "visible_excerpt": visible[:900],
    }


def _llm_source_classification(page_html: str, url: str, wp_type: str | None, title: str, hints: dict, heuristic: dict) -> dict:
    evidence = _classification_evidence(page_html, url, wp_type, title, hints, heuristic)
    fallback = {
        "source_type": heuristic.get("source_type", "article"),
        "product_kind": heuristic.get("product_kind", ""),
        "confidence": heuristic.get("confidence", 0.0),
        "reason": heuristic.get("reason", "heuristic fallback"),
    }
    try:
        llm_result = classifier.run(evidence, fallback=fallback)
    except Exception:
        llm_result = fallback
    merged = dict(heuristic)
    merged.update({
        "source_type": llm_result.get("source_type", heuristic.get("source_type", "article")),
        "source_kind": llm_result.get("source_type", heuristic.get("source_type", "article")),
        "product_kind": llm_result.get("product_kind", heuristic.get("product_kind", "")),
        "llm_confidence": llm_result.get("confidence", 0.0),
        "llm_reason": llm_result.get("reason", ""),
        "heuristic_source_type": heuristic.get("source_type"),
        "heuristic_product_kind": heuristic.get("product_kind"),
    })
    if merged["source_type"] != "product":
        merged["product_kind"] = ""
    return merged


def _extract_product_hints(page_html: str) -> dict:
    product_schema = _find_product_schema(page_html)
    hints: dict[str, list[str] | str] = {
        "meta_description": _extract_meta(page_html, "description") or _extract_meta(page_html, "og:description") or _strip_html(str(product_schema.get("description") or "")),
        "og_title": _extract_meta(page_html, "og:title") or _strip_html(str(product_schema.get("name") or "")),
        "components": [],
        "price_text": "",
        "sku": _strip_html(str(product_schema.get("sku") or "")),
        "category": _strip_html(str(product_schema.get("category") or "")),
        "weight_text": _strip_html(str((product_schema.get("weight") or {}).get("value") or "")),
        "variants": [],
    }
    visible = _strip_html(page_html)
    component_terms = re.findall(r"Trà\s+[A-ZÀ-Ỵa-zà-ỵ0-9]+(?:\s+[A-ZÀ-Ỵa-zà-ỵ0-9]+){0,3}", visible)
    components: list[str] = []
    seen: set[str] = set()
    blocked = ("trà việt", "giỏ hàng", "đăng nhập", "yêu thích", "so sánh", "var ")
    for term in component_terms:
        cleaned = re.sub(r"\s+", " ", term).strip(" .,:;|-")
        lowered = cleaned.lower()
        if any(token in lowered for token in blocked):
            continue
        if len(cleaned) < 6 or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        components.append(cleaned)
        if len(components) >= 10:
            break
    hints["components"] = components
    price_match = re.search(r"(\d[\d\.\,]{3,}\s*(?:₫|đ|VND))", visible, re.IGNORECASE)
    if price_match:
        hints["price_text"] = price_match.group(1)
    offers = product_schema.get("offers") if isinstance(product_schema, dict) else {}
    if not hints["price_text"] and isinstance(offers, dict):
        low = str(offers.get("lowPrice") or "").strip()
        high = str(offers.get("highPrice") or "").strip()
        currency = str(offers.get("priceCurrency") or "VND").strip()
        if low and high and low != high:
            hints["price_text"] = f"{low} - {high} {currency}".strip()
        elif low:
            hints["price_text"] = f"{low} {currency}".strip()
    variants = _extract_variants(page_html, offers)
    if variants:
        hints["variants"] = variants
    return hints


def _extract_variants(page_html: str, offers: object) -> list[dict[str, str]]:
    variants: list[dict[str, str]] = []
    if isinstance(offers, list):
        for offer in offers[:12]:
            if not isinstance(offer, dict):
                continue
            variant = {
                "name": _strip_html(str(offer.get("name") or offer.get("sku") or "")),
                "price": _strip_html(str(offer.get("price") or "")),
                "currency": _strip_html(str(offer.get("priceCurrency") or "")),
            }
            if variant["name"] or variant["price"]:
                variants.append(variant)
    option_scope = page_html
    form_match = re.search(r'<form[^>]+class=["\'][^"\']*variations_form[^"\']*["\'][^>]*>(.*?)</form>', page_html, re.IGNORECASE | re.DOTALL)
    if form_match:
        option_scope = form_match.group(1)
    pattern = r'<option[^>]+value=["\']([^"\']+)["\'][^>]*>(.*?)</option>'
    rating_labels = {"rất tốt", "tốt", "trung bình", "không tệ", "rất tệ"}
    for value, label in re.findall(pattern, option_scope, re.IGNORECASE | re.DOTALL):
        text = _strip_html(label)
        if not text or text.lower() in {"choose an option", "chọn một tùy chọn", "lựa chọn"}:
            continue
        if text.lower() in rating_labels or str(value).strip() in {"1", "2", "3", "4", "5"}:
            continue
        variants.append({"name": text, "value": _strip_html(value)})
        if len(variants) >= 12:
            break
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for variant in variants:
        key = " ".join(str(v) for v in variant.values()).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append({k: v for k, v in variant.items() if v})
    return unique[:12]


def _extract_product_body_text(page_html: str) -> str:
    snippets: list[str] = []
    patterns = [
        r'woocommerce-product-details__short-description[^>]*>(.*?)</div>',
        r'woocommerce-Tabs-panel--description[^>]*>(.*?)</div>',
        r'id="tab-description"[^>]*>(.*?)</div>',
        r'woocommerce-variation-description[^>]*>(.*?)</div>',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, page_html, re.IGNORECASE | re.DOTALL):
            text = _strip_html(match.group(1))
            if len(text.split()) >= 12:
                snippets.append(text)
    combined = "\n".join(snippets)
    combined = re.split(r"(Đánh giá|Nhận xét|Related Products|Sản phẩm tương tự)", combined, maxsplit=1, flags=re.IGNORECASE)[0]
    return re.sub(r"\s+", " ", combined).strip()


def _raw_html_fallback_result(downloaded: str, url: str, wp_type: str | None = None) -> dict:
    text = _strip_html(downloaded)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text.split()) < 35:
        raise RuntimeError(f"Trafilatura could not extract readable content from {url}")
    product_hints = _extract_product_hints(downloaded)
    title = (
        _extract_meta(downloaded, "og:title")
        or _extract_html_title(downloaded)
        or product_hints.get("og_title")
        or _title_from_url(url)
    )
    heuristic_classification = _detect_source_classification(downloaded, url, wp_type or "")
    classification = _llm_source_classification(downloaded, url, wp_type or "", title, product_hints, heuristic_classification)
    if product_hints.get("meta_description") and product_hints["meta_description"] not in text:
        text = f"{product_hints['meta_description']} {text}".strip()
    return _assert_real_content({
        "title": title,
        "clean_content": text,
        "html": downloaded,
        "metadata": {
            "author": urlparse(url).netloc,
            "publish_date": "",
            "url": url,
            "sitename": urlparse(url).netloc,
            "language": "vi",
            "featured_image_url": "",
            "featured_image_alt": "",
            "image_urls": _extract_image_urls(downloaded),
            "source_type": classification["source_type"],
            "source_kind": classification["source_kind"],
            "product_kind": classification["product_kind"],
            "source_classification": classification,
            "product_hints": product_hints,
        },
    }, url)


def _assert_real_content(result: dict, url: str) -> dict:
    clean_content = (result.get("clean_content") or "").strip()
    metadata = result.setdefault("metadata", {})
    hints = metadata.get("product_hints") or {}
    combined = " ".join(
        [
            clean_content,
            hints.get("meta_description", "") if isinstance(hints, dict) else "",
            " ".join(hints.get("components", [])) if isinstance(hints, dict) else "",
        ]
    ).strip()
    if len(combined.split()) < 35:
        raise RuntimeError(f"Fetcher extracted too little real content from {url}; sending job to DLQ instead of using mock data.")
    metadata["content_word_count"] = len(clean_content.split())
    return result


def _extract_image_urls(page_html: str, limit: int = 12) -> list[str]:
    candidates = re.findall(r"https?://[^\"'\s<>]+?\.(?:jpg|jpeg|png|webp)", page_html, re.IGNORECASE)
    images: list[str] = []
    seen: set[str] = set()
    seen_base: set[str] = set()
    skip_terms = ("logo", "icon", "favicon", "cropped", "avatar", "banner", "white-horizone")
    for raw_url in candidates:
        url = html.unescape(raw_url).split("?")[0].strip()
        lower = url.lower()
        if "/wp-content/uploads/" not in lower:
            continue
        if any(term in lower for term in skip_terms):
            continue
        if "-100x100." in lower or "-150x150." in lower or "-300x300." in lower:
            continue
        base_key = re.sub(r"-\d+x\d+(\.(?:jpg|jpeg|png|webp))$", r"\1", lower)
        if url in seen:
            continue
        if base_key in seen_base:
            continue
        seen.add(url)
        seen_base.add(base_key)
        images.append(url)
        if len(images) >= limit:
            break
    return images


def _fetch_json(url: str) -> dict | None:
    curl_body = _curl_get(url)
    if curl_body:
        try:
            return json.loads(curl_body)
        except json.JSONDecodeError:
            pass
    if httpx is None:
        return None
    try:
        response = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "ContentForge/2.0"})
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def run_seeded_product(seed: dict) -> dict:
    normalized = dict(seed.get("normalized") or {})
    raw = dict(seed.get("raw") or {})
    title = str(normalized.get("product_title") or raw.get("title") or "").strip() or _title_from_url(str(normalized.get("source_url") or raw.get("url") or ""))
    clean_content = str(normalized.get("seed_content") or normalized.get("description_text") or raw.get("description") or title).strip()
    image_urls = [str(url).strip() for url in (normalized.get("images") or raw.get("images") or []) if str(url).strip()]
    product_kind = "variable" if str(normalized.get("type") or "").lower() == "variable" else "simple"
    product_hints = {
        "meta_description": str(normalized.get("short_description") or ""),
        "og_title": title,
        "sku": str(normalized.get("item_id") or ""),
        "category": "Shopee",
        "variants": [str(item.get("name") or "") for item in (normalized.get("variations") or []) if str(item.get("name") or "").strip()],
        "components": [str(item.get("name") or "") for item in (normalized.get("attributes") or []) if str(item.get("name") or "").strip()],
        "price_text": str(normalized.get("regular_price") or ""),
    }
    return {
        "title": title,
        "clean_content": clean_content,
        "html": clean_content,
        "metadata": {
            "author": "Shopee",
            "publish_date": "",
            "url": str(normalized.get("source_url") or raw.get("url") or ""),
            "sitename": "Shopee",
            "language": "vi",
            "featured_image_url": image_urls[0] if image_urls else "",
            "featured_image_alt": title,
            "image_urls": image_urls,
            "source_type": "product",
            "source_kind": "product",
            "product_kind": product_kind,
            "source_classification": {
                "source_type": "product",
                "source_kind": "product",
                "product_kind": product_kind,
                "confidence": 1.0,
                "reason": "seeded raw product from Shopee dataset",
            },
            "product_hints": product_hints,
        },
    }


def _fetch_wordpress_content(url: str) -> dict | None:
    fetch_url = _canonical_fetch_url(url)
    page_html = _curl_get(fetch_url)
    if not page_html and httpx is not None:
        try:
            page = httpx.get(fetch_url, timeout=30, follow_redirects=True, headers={"User-Agent": "ContentForge/2.0"})
            page.raise_for_status()
            page_html = page.text
        except Exception:
            return None
    if not page_html:
        return None
    api_root = _extract_wordpress_api_url(page_html)
    if not api_root:
        return None

    shortlink = re.search(r'<link[^>]+rel=shortlink[^>]+href="[^"]+[?&]p=(\d+)"', page_html, re.IGNORECASE)
    product_api = re.search(r'<link[^>]+type="application/json"[^>]+href="([^"]*/wp-json/wp/v2/product/(\d+))"', page_html, re.IGNORECASE)
    api_url = None
    if product_api:
        api_url = product_api.group(1)
    elif shortlink:
        api_url = api_root.rstrip("/") + f"/wp/v2/posts/{shortlink.group(1)}"
    if not api_url:
        return None

    data = _fetch_json(api_url)
    if not data:
        return None

    featured_image_url = ""
    featured_image_alt = ""
    featured_media = data.get("featured_media")
    if featured_media:
        media_data = _fetch_json(api_root.rstrip("/") + f"/wp/v2/media/{featured_media}")
        if media_data:
            featured_image_url = media_data.get("source_url", "")
            featured_image_alt = media_data.get("alt_text", "")

    product_hints = _extract_product_hints(page_html)
    product_schema = _find_product_schema(page_html)
    title = html.unescape(_strip_html(data.get("title", {}).get("rendered", ""))) or product_hints.get("og_title") or _strip_html(str(product_schema.get("name") or "")) or _title_from_url(url)
    heuristic_classification = _detect_source_classification(page_html, url, data.get("type", ""))
    classification = _llm_source_classification(page_html, url, data.get("type", ""), title, product_hints, heuristic_classification)
    content_html = data.get("content", {}).get("rendered", "") or data.get("excerpt", {}).get("rendered", "")
    excerpt_html = data.get("excerpt", {}).get("rendered", "")
    body_text = _extract_product_body_text(page_html)
    schema_text = _strip_html(str(product_schema.get("description") or ""))
    clean_parts: list[str] = []
    for candidate in [schema_text, body_text, _strip_html(excerpt_html), _strip_html(content_html)]:
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if candidate and candidate not in clean_parts:
            clean_parts.append(candidate)
    clean_content = " ".join(clean_parts).strip()
    if product_hints.get("meta_description") and product_hints["meta_description"] not in clean_content:
        clean_content = f"{product_hints['meta_description']} {clean_content}".strip()
    image_urls = _extract_image_urls(page_html)
    if featured_image_url and featured_image_url not in image_urls:
        image_urls.insert(0, featured_image_url)

    return {
        "title": title,
        "clean_content": clean_content,
        "html": content_html or f"<p>{clean_content}</p>",
        "metadata": {
            "author": urlparse(url).netloc,
            "publish_date": (data.get("date") or "")[:10],
            "url": url,
            "sitename": urlparse(url).netloc,
            "language": "vi",
            "source_api_url": api_url,
            "featured_image_url": featured_image_url,
            "featured_image_alt": featured_image_alt,
            "image_urls": image_urls,
            "source_type": classification["source_type"],
            "source_kind": classification["source_kind"],
            "product_kind": classification["product_kind"],
            "source_classification": classification,
            "product_hints": product_hints,
        },
    }


def run(url: str) -> dict:
    wordpress_result = _fetch_wordpress_content(url)
    if wordpress_result:
        return _assert_real_content(wordpress_result, url)

    if trafilatura is None:
        raise RuntimeError("trafilatura is not installed and WordPress extraction did not succeed.")

    downloaded = trafilatura.fetch_url(_canonical_fetch_url(url))
    if not downloaded:
        raise RuntimeError(f"Could not fetch URL: {url}")

    extracted = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        output_format="json",
        with_metadata=True,
    )
    if not extracted:
        return _raw_html_fallback_result(downloaded, url)

    data = json.loads(extracted)
    text = data.get("text", "") or data.get("raw_text", "")
    html = data.get("html", "") or f"<p>{text}</p>"
    image_urls = _extract_image_urls(downloaded)
    product_hints = _extract_product_hints(downloaded)
    title = data.get("title") or product_hints.get("og_title") or _title_from_url(url)
    heuristic_classification = _detect_source_classification(downloaded, url, data.get("type", ""))
    classification = _llm_source_classification(downloaded, url, data.get("type", ""), title, product_hints, heuristic_classification)
    if product_hints.get("meta_description") and product_hints["meta_description"] not in text:
        text = f"{text} {product_hints['meta_description']}".strip()
    return _assert_real_content({
        "title": title,
        "clean_content": text,
        "html": html,
        "metadata": {
            "author": data.get("author") or data.get("sitename") or "",
            "publish_date": data.get("date") or "",
            "url": url,
            "sitename": data.get("sitename") or urlparse(url).netloc,
            "language": data.get("language") or "vi",
            "featured_image_url": "",
            "featured_image_alt": "",
            "image_urls": image_urls,
            "source_type": classification["source_type"],
            "source_kind": classification["source_kind"],
            "product_kind": classification["product_kind"],
            "source_classification": classification,
            "product_hints": product_hints,
        },
    }, url)
