from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from app.postgres import get_connection as _pg_connection, init_schema as _init_postgres_schema, postgres_available, serialize_json


DATA_PATH = Path("data/shopee_products.json")
STORE_LOCK = Lock()
SAMPLE_PATH = Path("woo.json")


def _postgres_conn():
    if postgres_available():
        _init_postgres_schema()
    return _pg_connection() if postgres_available() else None


def _ensure_store() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        DATA_PATH.write_text("[]", encoding="utf-8")


def _load_products() -> list[dict[str, Any]]:
    _ensure_store()
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = []
    return payload if isinstance(payload, list) else []


def _save_products(items: list[dict[str, Any]]) -> None:
    _ensure_store()
    DATA_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _safe_int(value: Any) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if numeric <= 0:
        return 0
    if numeric > 1_000_000_000:
        return 0
    return int(numeric)


def _strip_markup(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"[_*`#]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clean_description(text: str) -> str:
    text = _strip_markup(text)
    patterns = [
        r"CAM KẾT.*",
        r"CHÍNH SÁCH.*",
        r"QUÝ KHÁCH.*",
        r"LIÊN HỆ NGAY SHOP.*",
        r"#\S+",
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" -,:;")


def _slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return (re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-") or "shopee-product")[:96]


def _variant_attributes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    attributes: list[dict[str, Any]] = []
    for item in raw.get("tierVariations") or []:
        name = str(item.get("name") or "").strip()
        options = [str(option).strip() for option in (item.get("options") or []) if str(option).strip()]
        if name and options:
            attributes.append({"name": name, "visible": True, "variation": True, "options": options})
    return attributes


def _product_attributes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    attributes = _variant_attributes(raw)
    variation_names = {item["name"].lower() for item in attributes}
    for item in raw.get("attributes") or []:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name or not value or name.lower() in variation_names:
            continue
        attributes.append(
            {
                "name": name,
                "visible": True,
                "variation": False,
                "options": [part.strip() for part in re.split(r",\s*", value) if part.strip()] or [value],
            }
        )
    return attributes


def _price_summary(raw: dict[str, Any]) -> tuple[int, int | None]:
    variants = raw.get("variants") or []
    prices = sorted(_safe_int(item.get("price")) for item in variants if _safe_int(item.get("price")) > 0)
    compare = sorted(_safe_int(item.get("priceBeforeDiscount")) for item in variants if _safe_int(item.get("priceBeforeDiscount")) > 0)
    if prices:
        return prices[0], compare[0] if compare and compare[0] > prices[0] else None
    regular = _safe_int(raw.get("price"))
    return regular, None


def _variations(raw: dict[str, Any], attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    variation_names = [item["name"] for item in attributes if item.get("variation")]
    tier_variations = raw.get("tierVariations") or []
    output: list[dict[str, Any]] = []
    for variant in raw.get("variants") or []:
        mapped_attributes: dict[str, str] = {}
        tier_index = variant.get("tierIndex") or []
        for idx, attr_name in enumerate(variation_names):
            if idx >= len(tier_variations) or idx >= len(tier_index):
                continue
            options = tier_variations[idx].get("options") or []
            option_index = tier_index[idx]
            if isinstance(option_index, int) and 0 <= option_index < len(options):
                mapped_attributes[attr_name] = str(options[option_index]).strip()
        output.append(
            {
                "model_id": str(variant.get("modelId") or ""),
                "name": str(variant.get("name") or "").strip(),
                "regular_price": _safe_int(variant.get("price")),
                "sale_price": _safe_int(variant.get("priceBeforeDiscount")) or None,
                "stock": int(variant.get("stock") or 0),
                "image": str(variant.get("image") or "").strip(),
                "attributes": mapped_attributes,
            }
        )
    return output


def _seed_content(raw: dict[str, Any], normalized: dict[str, Any]) -> str:
    lines = [
        normalized.get("product_title") or "",
        raw.get("shortDescription") or "",
        normalized.get("description_text") or "",
    ]
    for item in normalized.get("attributes") or []:
        options = ", ".join(str(option) for option in (item.get("options") or [])[:8])
        if options:
            lines.append(f"{item.get('name')}: {options}")
    for variation in normalized.get("variations") or []:
        attrs = ", ".join(f"{key}: {value}" for key, value in (variation.get("attributes") or {}).items())
        lines.append(f"Biến thể {variation.get('name')}: {attrs}")
    return "\n".join(part for part in lines if part).strip()


def normalize_shopee_product(raw: dict[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    regular_price, sale_price = _price_summary(raw)
    attributes = _product_attributes(raw)
    variations = _variations(raw, attributes)
    normalized = {
        "source": "shopee",
        "item_id": str(raw.get("itemId") or ""),
        "shop_id": str(raw.get("shopId") or ""),
        "source_url": str(raw.get("url") or "").strip(),
        "product_title": title,
        "product_slug": _slugify(title),
        "type": "variable" if variations else "simple",
        "regular_price": regular_price,
        "sale_price": sale_price,
        "short_description": _strip_markup(str(raw.get("shortDescription") or title)),
        "description_text": _clean_description(str(raw.get("description") or "")),
        "images": [str(url).strip() for url in (raw.get("images") or []) if str(url).strip()],
        "attributes": attributes,
        "variations": variations,
        "raw_variant_count": int(raw.get("variantCount") or len(variations) or 0),
        "rating": raw.get("rating"),
        "currency": str(raw.get("currency") or "VND"),
    }
    normalized["seed_content"] = _seed_content(raw, normalized)
    return normalized


def _product_record(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_shopee_product(raw)
    return {
        "item_id": normalized["item_id"],
        "source": "shopee",
        "raw": raw,
        "normalized": normalized,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def upsert_shopee_product(raw: dict[str, Any]) -> dict[str, Any]:
    record = _product_record(raw)
    item_id = record["item_id"]
    if not item_id:
        raise ValueError("Shopee product itemId is required")

    pg_conn = _postgres_conn()
    if pg_conn is not None:
        existing = get_shopee_product(item_id)
        if existing:
            record["created_at"] = existing.get("created_at") or record["created_at"]
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shopee_products (item_id, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                ON CONFLICT (item_id) DO UPDATE SET
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (item_id, serialize_json(record)),
            )
        return record

    with STORE_LOCK:
        items = _load_products()
        replaced = False
        for index, item in enumerate(items):
            if str(item.get("item_id") or "") != item_id:
                continue
            record["created_at"] = item.get("created_at") or record["created_at"]
            items[index] = record
            replaced = True
            break
        if not replaced:
            items.append(record)
        _save_products(items)
    return record


def get_shopee_product(item_id: str) -> dict[str, Any] | None:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM shopee_products WHERE item_id = %s", (item_id,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
    with STORE_LOCK:
        for item in _load_products():
            if str(item.get("item_id") or "") == item_id:
                return item
    return None


def list_shopee_products(search: str | None = None, limit: int = 100) -> dict[str, Any]:
    if postgres_available():
        pg_conn = _postgres_conn()
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM shopee_products ORDER BY updated_at DESC LIMIT %s", (max(1, min(limit * 4, 1000)),))
            records = [json.loads(row[0]) for row in cur.fetchall()]
    else:
        with STORE_LOCK:
            records = list(_load_products())

    search_lower = (search or "").strip().lower()
    filtered: list[dict[str, Any]] = []
    for record in records:
        normalized = record.get("normalized") or {}
        haystack = " ".join(
            [
                str(normalized.get("product_title") or ""),
                str(normalized.get("source_url") or ""),
                str(record.get("item_id") or ""),
            ]
        ).lower()
        if search_lower and search_lower not in haystack:
            continue
        filtered.append(record)

    items = []
    for record in filtered[: max(1, min(limit, 500))]:
        normalized = record.get("normalized") or {}
        items.append(
            {
                "item_id": str(record.get("item_id") or ""),
                "shop_id": str(normalized.get("shop_id") or ""),
                "title": str(normalized.get("product_title") or ""),
                "type": str(normalized.get("type") or "simple"),
                "regular_price": int(normalized.get("regular_price") or 0),
                "sale_price": normalized.get("sale_price"),
                "variant_count": int(normalized.get("raw_variant_count") or 0),
                "image_count": len(normalized.get("images") or []),
                "image_url": str((normalized.get("images") or [""])[0] or ""),
                "url": str(normalized.get("source_url") or ""),
                "updated_at": str(record.get("updated_at") or ""),
            }
        )
    return {
        "source_url": "chrome-extension",
        "category_label": "Shopee normalized catalog",
        "total": len(items),
        "items": items,
    }


def import_legacy_sample() -> dict[str, int]:
    if not SAMPLE_PATH.exists():
        return {"imported": 0}
    try:
        payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"imported": 0}
    imported = 0
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        upsert_shopee_product(item)
        imported += 1
    return {"imported": imported}
