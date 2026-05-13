from __future__ import annotations

import json
import re
import secrets
import unicodedata
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from app.chroma import add_documents, delete_documents, get_documents, get_named_collection_name, search_documents
from app.postgres import get_connection as _pg_connection, init_schema as _init_postgres_schema, postgres_available, serialize_json


PRODUCTS_PATH = Path("data/chatbot_products.json")
CATEGORIES_PATH = Path("data/chatbot_product_categories.json")
LABELS_PATH = Path("data/chatbot_product_labels.json")
STORE_LOCK = Lock()


def _postgres_conn():
    if postgres_available():
        _init_postgres_schema()
    return _pg_connection() if postgres_available() else None


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _ensure_store() -> None:
    PRODUCTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PRODUCTS_PATH.exists():
        PRODUCTS_PATH.write_text("[]", encoding="utf-8")
    if not CATEGORIES_PATH.exists():
        CATEGORIES_PATH.write_text("[]", encoding="utf-8")
    if not LABELS_PATH.exists():
        LABELS_PATH.write_text("[]", encoding="utf-8")


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    _ensure_store()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = []
    return payload if isinstance(payload, list) else []


def _save_json_list(path: Path, items: list[dict[str, Any]]) -> None:
    _ensure_store()
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-") or secrets.token_hex(4)


def _clean_text(value: Any, limit: int = 5000) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _safe_int(value: Any) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if numeric <= 0:
        return 0
    return int(numeric)


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[\n,]+", value)
    elif isinstance(value, list):
        parts = value
    else:
        parts = []
    output: list[str] = []
    seen: set[str] = set()
    for item in parts:
        text = _clean_text(item, 500)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output


def _label_key(value: str) -> str:
    return _clean_text(value, 120).lower()


def _remember_labels(labels: list[str]) -> None:
    cleaned = [label for label in _clean_list(labels) if label]
    if not cleaned:
        return
    now = _now_iso()
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            for label in cleaned:
                key = _label_key(label)
                payload = {"label": label, "key": key, "updated_at": now}
                cur.execute(
                    """
                    INSERT INTO chatbot_product_labels (label, updated_at, data)
                    VALUES (%s, NOW(), %s::jsonb)
                    ON CONFLICT (label) DO UPDATE SET updated_at = NOW(), data = EXCLUDED.data
                    """,
                    (key, serialize_json(payload)),
                )
        return
    with STORE_LOCK:
        existing = {str(item.get("key") or item.get("label") or "").lower(): item for item in _load_json_list(LABELS_PATH)}
        for label in cleaned:
            key = _label_key(label)
            existing[key] = {"label": label, "key": key, "updated_at": now}
        _save_json_list(LABELS_PATH, list(existing.values()))


def list_labels(search: str | None = None, limit: int = 100) -> dict[str, Any]:
    needle = (search or "").strip().lower()
    if postgres_available():
        conn = _postgres_conn()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM chatbot_product_labels ORDER BY updated_at DESC LIMIT %s", (max(1, min(limit * 3, 1000)),))
            labels = [json.loads(row[0]) for row in cur.fetchall()]
    else:
        with STORE_LOCK:
            labels = _load_json_list(LABELS_PATH)
    output = []
    for item in labels:
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        if needle and needle not in label.lower():
            continue
        output.append(item)
    return {"total": len(output), "labels": output[: max(1, min(limit, 300))]}


def _availability(is_active: bool) -> str:
    return "available" if is_active else "unavailable"


def _catalog_collection_name() -> str:
    return get_named_collection_name("chatbot_products")


def _metadata_csv(values: Any) -> str:
    if isinstance(values, dict):
        return ", ".join(f"{key}: {val}" for key, val in values.items() if str(val).strip())
    return ", ".join(_clean_list(values))


def _image_summary_lines(items: Any) -> list[str]:
    if isinstance(items, str):
        return [items] if items.strip() else []
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items:
        if isinstance(item, dict):
            url = str(item.get("image_url") or item.get("url") or "").strip()
            summary = str(item.get("summary") or item.get("description") or "").strip()
            keywords = _metadata_csv(item.get("visual_keywords") or item.get("keywords") or [])
            parts = [part for part in [url, summary, keywords] if part]
            if parts:
                lines.append(" | ".join(parts))
        elif str(item or "").strip():
            lines.append(str(item).strip())
    return lines


def _product_document(product: dict[str, Any]) -> str:
    variants = product.get("variants") or []
    variant_lines = []
    for variant in variants:
        status = "còn hàng" if variant.get("is_active", True) else "hết hàng"
        attrs = _metadata_csv(variant.get("attributes") or {})
        variant_lines.append(f"- {variant.get('name')}: {variant.get('price')} {variant.get('currency') or product.get('currency')}; {attrs}; trạng thái {status}")
    status = "còn hàng" if product.get("is_active", True) else "hết hàng"
    lines = [
        f"Tên sản phẩm: {product.get('title')}",
        f"Trạng thái tư vấn: {status}",
        f"Giá: {product.get('price')} {product.get('currency')}",
        f"Danh mục: {product.get('category_name')}",
        f"Thương hiệu: {product.get('brand')}",
        f"Nhãn: {', '.join(product.get('labels') or [])}",
        f"Mô tả ngắn: {product.get('short_description')}",
        f"Mô tả chi tiết: {product.get('description')}",
        f"Thuộc tính: {_metadata_csv(product.get('attributes') or {})}",
        f"Ảnh: {', '.join(product.get('images') or [])}",
        f"Mô tả thị giác ảnh sản phẩm: {'; '.join(_image_summary_lines(product.get('image_summaries')))}",
        "Biến thể và trạng thái:",
        "\n".join(variant_lines),
    ]
    return "\n".join(str(line or "").strip() for line in lines if str(line or "").strip())


def _variant_document(product: dict[str, Any], variant: dict[str, Any]) -> str:
    status = "còn hàng" if variant.get("is_active", True) else "hết hàng"
    lines = [
        f"Sản phẩm: {product.get('title')}",
        f"Biến thể: {variant.get('name')}",
        f"Trạng thái tư vấn: {status}",
        f"Giá biến thể: {variant.get('price')} {variant.get('currency') or product.get('currency')}",
        f"Danh mục: {product.get('category_name')}",
        f"Nhãn sản phẩm: {', '.join(product.get('labels') or [])}",
        f"Nhãn biến thể: {', '.join(variant.get('labels') or [])}",
        f"Thuộc tính biến thể: {_metadata_csv(variant.get('attributes') or {})}",
        f"Ảnh biến thể: {variant.get('image_url')}",
        f"Mô tả thị giác ảnh biến thể: {variant.get('image_summary')}",
        f"Mô tả sản phẩm: {product.get('short_description') or product.get('description')}",
    ]
    return "\n".join(str(line or "").strip() for line in lines if str(line or "").strip())


def _product_documents(product: dict[str, Any]) -> list[dict[str, Any]]:
    product_id = str(product.get("product_id") or "").strip()
    if not product_id:
        return []
    base_metadata = {
        "source": "chatbot_catalog",
        "product_id": product_id,
        "title": str(product.get("title") or ""),
        "category_id": str(product.get("category_id") or ""),
        "category_name": str(product.get("category_name") or ""),
        "brand": str(product.get("brand") or ""),
        "labels": ", ".join(product.get("labels") or []),
        "image_summaries": " | ".join(_image_summary_lines(product.get("image_summaries"))),
        "currency": str(product.get("currency") or "VND"),
        "availability_status": str(product.get("availability_status") or _availability(product.get("is_active", True))),
        "is_active": bool(product.get("is_active", True)),
        "product_url": str(product.get("product_url") or ""),
        "updated_at": str(product.get("updated_at") or ""),
    }
    docs = [
        {
            "id": f"chatbot_product_{product_id}",
            "document": _product_document(product),
            "metadata": {**base_metadata, "doc_type": "product", "variant_id": "", "price": int(product.get("price") or 0)},
        }
    ]
    for variant in product.get("variants") or []:
        variant_id = str(variant.get("variant_id") or "").strip()
        if not variant_id:
            continue
        docs.append(
            {
                "id": f"chatbot_variant_{product_id}_{variant_id}",
                "document": _variant_document(product, variant),
                "metadata": {
                    **base_metadata,
                    "doc_type": "variant",
                    "variant_id": variant_id,
                    "variant_name": str(variant.get("name") or ""),
                    "price": int(variant.get("price") or 0),
                    "availability_status": str(variant.get("availability_status") or _availability(variant.get("is_active", True))),
                    "is_active": bool(variant.get("is_active", True)),
                    "attributes": _metadata_csv(variant.get("attributes") or {}),
                    "image_url": str(variant.get("image_url") or ""),
                },
            }
        )
    return docs


def _normalize_category(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    name = _clean_text(payload.get("name") or existing.get("name"), 160)
    if not name:
        raise ValueError("Category name is required")
    category_id = _clean_text(payload.get("category_id") or existing.get("category_id"), 120) or f"cat_{secrets.token_hex(6)}"
    slug = _clean_text(payload.get("slug") or existing.get("slug"), 160) or _slugify(name)
    now = _now_iso()
    return {
        "category_id": category_id,
        "name": name,
        "slug": slug,
        "description": _clean_text(payload.get("description") if "description" in payload else existing.get("description"), 1000),
        "icon": _clean_text(payload.get("icon") if "icon" in payload else existing.get("icon"), 80),
        "sort_order": _safe_int(payload.get("sort_order") if "sort_order" in payload else existing.get("sort_order")),
        "is_active": bool(payload.get("is_active", existing.get("is_active", True))),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }


def _normalize_variant(raw: dict[str, Any], index: int = 0, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    is_active = bool(raw.get("is_active", existing.get("is_active", True)))
    name = _clean_text(raw.get("name") or existing.get("name") or f"Biến thể {index + 1}", 180)
    variant_id = _clean_text(raw.get("variant_id") or existing.get("variant_id"), 140) or f"var_{secrets.token_hex(6)}"
    labels = _clean_list(raw.get("labels") if "labels" in raw else existing.get("labels"))
    attributes = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else existing.get("attributes") or {}
    return {
        "variant_id": variant_id,
        "name": name,
        "sku": _clean_text(raw.get("sku") if "sku" in raw else existing.get("sku"), 120),
        "price": _safe_int(raw.get("price") if "price" in raw else existing.get("price")),
        "compare_at_price": _safe_int(raw.get("compare_at_price") if "compare_at_price" in raw else existing.get("compare_at_price")),
        "currency": _clean_text(raw.get("currency") or existing.get("currency") or "VND", 12).upper(),
        "image_url": _clean_text(raw.get("image_url") or raw.get("image") or existing.get("image_url"), 1200),
        "image_summary": _clean_text(raw.get("image_summary") if "image_summary" in raw else existing.get("image_summary"), 4000),
        "attributes": attributes,
        "labels": labels,
        "is_active": is_active,
        "availability_status": raw.get("availability_status") or _availability(is_active),
        "sort_order": _safe_int(raw.get("sort_order") if "sort_order" in raw else existing.get("sort_order") or index),
    }


def _normalize_product(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    title = _clean_text(payload.get("title") or existing.get("title"), 240)
    if not title:
        raise ValueError("Product title is required")
    product_id = _clean_text(payload.get("product_id") or existing.get("product_id"), 140) or f"prd_{secrets.token_hex(8)}"
    is_active = bool(payload.get("is_active", existing.get("is_active", True)))
    raw_variants = payload.get("variants") if "variants" in payload else existing.get("variants", [])
    variants = [
        _normalize_variant(item, index)
        for index, item in enumerate(raw_variants if isinstance(raw_variants, list) else [])
        if isinstance(item, dict)
    ]
    now = _now_iso()
    category_id = _clean_text(payload.get("category_id") if "category_id" in payload else existing.get("category_id"), 140)
    return {
        "product_id": product_id,
        "title": title,
        "slug": _clean_text(payload.get("slug") or existing.get("slug"), 180) or _slugify(title),
        "sku": _clean_text(payload.get("sku") if "sku" in payload else existing.get("sku"), 120),
        "short_description": _clean_text(payload.get("short_description") if "short_description" in payload else existing.get("short_description"), 1000),
        "description": _clean_text(payload.get("description") if "description" in payload else existing.get("description"), 8000),
        "price": _safe_int(payload.get("price") if "price" in payload else existing.get("price")),
        "compare_at_price": _safe_int(payload.get("compare_at_price") if "compare_at_price" in payload else existing.get("compare_at_price")),
        "currency": _clean_text(payload.get("currency") or existing.get("currency") or "VND", 12).upper(),
        "category_id": category_id,
        "category_name": _clean_text(payload.get("category_name") if "category_name" in payload else existing.get("category_name"), 160),
        "brand": _clean_text(payload.get("brand") if "brand" in payload else existing.get("brand"), 160),
        "labels": _clean_list(payload.get("labels") if "labels" in payload else existing.get("labels")),
        "images": _clean_list(payload.get("images") if "images" in payload else existing.get("images")),
        "image_summaries": payload.get("image_summaries") if isinstance(payload.get("image_summaries"), list) else existing.get("image_summaries") or [],
        "attributes": payload.get("attributes") if isinstance(payload.get("attributes"), dict) else existing.get("attributes") or {},
        "variants": variants,
        "is_active": is_active,
        "availability_status": payload.get("availability_status") or _availability(is_active),
        "product_url": _clean_text(payload.get("product_url") if "product_url" in payload else existing.get("product_url"), 1200),
        "source": _clean_text(payload.get("source") or existing.get("source") or "manual", 80),
        "source_id": _clean_text(payload.get("source_id") if "source_id" in payload else existing.get("source_id"), 180),
        "rag_dirty": bool(payload.get("rag_dirty", True)),
        "data": payload.get("data") if isinstance(payload.get("data"), dict) else existing.get("data") or {},
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }


def _category_counts(products: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for product in products:
        category_id = str(product.get("category_id") or "")
        counts[category_id] = counts.get(category_id, 0) + 1
    return counts


def list_categories(search: str | None = None) -> dict[str, Any]:
    products = list_products(limit=10000).get("items", [])
    counts = _category_counts(products)
    if postgres_available():
        conn = _postgres_conn()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM chatbot_product_categories ORDER BY data->>'name' ASC")
            categories = [json.loads(row[0]) for row in cur.fetchall()]
    else:
        with STORE_LOCK:
            categories = _load_json_list(CATEGORIES_PATH)
    needle = (search or "").strip().lower()
    output = []
    for item in categories:
        if needle and needle not in f"{item.get('name')} {item.get('slug')}".lower():
            continue
        item = dict(item)
        item["product_count"] = counts.get(str(item.get("category_id") or ""), 0)
        output.append(item)
    return {"total": len(output), "categories": output}


def upsert_category(payload: dict[str, Any], category_id: str = "") -> dict[str, Any]:
    existing = get_category(category_id or str(payload.get("category_id") or "")) if (category_id or payload.get("category_id")) else None
    item = _normalize_category({**payload, "category_id": category_id or payload.get("category_id") or ""}, existing)
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chatbot_product_categories (category_id, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                ON CONFLICT (category_id) DO UPDATE SET updated_at = NOW(), data = EXCLUDED.data
                """,
                (item["category_id"], serialize_json(item)),
            )
        return item
    with STORE_LOCK:
        items = [cat for cat in _load_json_list(CATEGORIES_PATH) if cat.get("category_id") != item["category_id"]]
        items.append(item)
        _save_json_list(CATEGORIES_PATH, items)
    return item


def get_category(category_id: str) -> dict[str, Any] | None:
    category_id = str(category_id or "").strip()
    if not category_id:
        return None
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM chatbot_product_categories WHERE category_id = %s", (category_id,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
    with STORE_LOCK:
        return next((item for item in _load_json_list(CATEGORIES_PATH) if item.get("category_id") == category_id), None)


def delete_category(category_id: str) -> bool:
    category_id = str(category_id or "").strip()
    if not category_id:
        return False
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chatbot_product_categories WHERE category_id = %s", (category_id,))
            return cur.rowcount > 0
    with STORE_LOCK:
        items = _load_json_list(CATEGORIES_PATH)
        remaining = [item for item in items if item.get("category_id") != category_id]
        if len(remaining) == len(items):
            return False
        _save_json_list(CATEGORIES_PATH, remaining)
    return True


def list_products(search: str | None = None, category_id: str | None = None, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    if postgres_available():
        conn = _postgres_conn()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM chatbot_products ORDER BY updated_at DESC LIMIT %s", (max(1, min(limit * 4, 2000)),))
            products = [json.loads(row[0]) for row in cur.fetchall()]
    else:
        with STORE_LOCK:
            products = _load_json_list(PRODUCTS_PATH)

    needle = (search or "").strip().lower()
    category_id = (category_id or "").strip()
    status = (status or "").strip().lower()
    output: list[dict[str, Any]] = []
    for item in products:
        haystack = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("sku") or ""),
                str(item.get("brand") or ""),
                " ".join(item.get("labels") or []),
            ]
        ).lower()
        if needle and needle not in haystack:
            continue
        if category_id and str(item.get("category_id") or "") != category_id:
            continue
        if status == "available" and not item.get("is_active", True):
            continue
        if status == "unavailable" and item.get("is_active", True):
            continue
        output.append(item)
    total = len(output)
    items = output[: max(1, min(limit, 500))]
    active = sum(1 for item in output if item.get("is_active", True))
    variants = sum(len(item.get("variants") or []) for item in output)
    return {
        "total": total,
        "items": items,
        "stats": {
            "total": total,
            "active": active,
            "unavailable": total - active,
            "variants": variants,
            "rag_dirty": sum(1 for item in output if item.get("rag_dirty")),
        },
    }


def get_product(product_id: str) -> dict[str, Any] | None:
    product_id = str(product_id or "").strip()
    if not product_id:
        return None
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM chatbot_products WHERE product_id = %s", (product_id,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
    with STORE_LOCK:
        return next((item for item in _load_json_list(PRODUCTS_PATH) if item.get("product_id") == product_id), None)


def upsert_product(payload: dict[str, Any], product_id: str = "") -> dict[str, Any]:
    existing = get_product(product_id or str(payload.get("product_id") or "")) if (product_id or payload.get("product_id")) else None
    item = _normalize_product({**payload, "product_id": product_id or payload.get("product_id") or ""}, existing)
    if item.get("category_id") and not item.get("category_name"):
        category = get_category(str(item.get("category_id") or ""))
        if category:
            item["category_name"] = category.get("name") or ""
    labels = list(item.get("labels") or [])
    for variant in item.get("variants") or []:
        labels.extend(variant.get("labels") or [])
    _remember_labels(labels)
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chatbot_products (product_id, category_id, status, updated_at, data)
                VALUES (%s, %s, %s, NOW(), %s::jsonb)
                ON CONFLICT (product_id) DO UPDATE
                SET category_id = EXCLUDED.category_id,
                    status = EXCLUDED.status,
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (item["product_id"], item.get("category_id") or "", item.get("availability_status") or "available", serialize_json(item)),
            )
        return item
    with STORE_LOCK:
        items = [product for product in _load_json_list(PRODUCTS_PATH) if product.get("product_id") != item["product_id"]]
        items.append(item)
        _save_json_list(PRODUCTS_PATH, items)
    return item


def delete_product(product_id: str) -> bool:
    product_id = str(product_id or "").strip()
    if not product_id:
        return False
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chatbot_products WHERE product_id = %s", (product_id,))
            return cur.rowcount > 0
    with STORE_LOCK:
        items = _load_json_list(PRODUCTS_PATH)
        remaining = [item for item in items if item.get("product_id") != product_id]
        if len(remaining) == len(items):
            return False
        _save_json_list(PRODUCTS_PATH, remaining)
    return True


def toggle_product(product_id: str, is_active: bool) -> dict[str, Any]:
    item = get_product(product_id)
    if not item:
        raise KeyError("Product not found")
    item["is_active"] = bool(is_active)
    item["availability_status"] = _availability(bool(is_active))
    item["rag_dirty"] = True
    return upsert_product(item, product_id)


def toggle_variant(product_id: str, variant_id: str, is_active: bool) -> dict[str, Any]:
    item = get_product(product_id)
    if not item:
        raise KeyError("Product not found")
    found = False
    for variant in item.get("variants") or []:
        if str(variant.get("variant_id") or "") == str(variant_id or ""):
            variant["is_active"] = bool(is_active)
            variant["availability_status"] = _availability(bool(is_active))
            found = True
            break
    if not found:
        raise KeyError("Variant not found")
    item["rag_dirty"] = True
    return upsert_product(item, product_id)


def mark_product_rag_clean(product: dict[str, Any]) -> None:
    product["rag_dirty"] = False
    upsert_product(product, str(product.get("product_id") or ""))


def reindex_catalog(product_id: str | None = None, dirty_only: bool = False) -> dict[str, Any]:
    collection_name = _catalog_collection_name()
    if product_id:
        products = [get_product(product_id)]
        products = [item for item in products if item]
    else:
        products = list_products(limit=10000).get("items", [])
    if dirty_only:
        products = [item for item in products if item.get("rag_dirty")]

    indexed_products = 0
    indexed_documents = 0
    deleted_documents = 0
    errors: list[str] = []
    for product in products:
        try:
            pid = str(product.get("product_id") or "")
            deleted_documents += delete_documents(where={"product_id": pid}, collection_name=collection_name)
            docs = _product_documents(product)
            add_documents(docs, collection_name=collection_name)
            product["rag_dirty"] = False
            mark_product_rag_clean(product)
            indexed_products += 1
            indexed_documents += len(docs)
        except Exception as error:  # pragma: no cover - surfaced in API response
            errors.append(f"{product.get('product_id')}: {error}")

    return {
        "collection": collection_name,
        "indexed_products": indexed_products,
        "indexed_documents": indexed_documents,
        "deleted_documents": deleted_documents,
        "errors": errors,
        "total": len(products),
    }


def delete_catalog_product_vectors(product_id: str) -> dict[str, Any]:
    collection_name = _catalog_collection_name()
    return {
        "collection": collection_name,
        "product_id": product_id,
        "deleted_documents": delete_documents(where={"product_id": str(product_id)}, collection_name=collection_name),
    }


def search_catalog(query: str, limit: int = 8, available_only: bool = False) -> dict[str, Any]:
    where = {"availability_status": "available"} if available_only else None
    results = search_documents(query, n_results=max(1, min(limit, 30)), where=where, collection_name=_catalog_collection_name())
    return {"query": query, "total": len(results), "results": results}


def catalog_rag_status() -> dict[str, Any]:
    collection_name = _catalog_collection_name()
    docs = get_documents(collection_name=collection_name)
    products = list_products(limit=10000).get("items", [])
    return {
        "collection": collection_name,
        "document_count": len(docs),
        "product_count": len(products),
        "rag_dirty": sum(1 for item in products if item.get("rag_dirty")),
    }
