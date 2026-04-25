from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

from app.config import get_settings
from app.postgres import get_connection as _pg_connection, postgres_available, serialize_json


DATA_PATH = Path("data/facebook_pages.json")
STORE_LOCK = Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_store() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        DATA_PATH.write_text("[]", encoding="utf-8")


def _load_pages() -> list[dict[str, Any]]:
    _ensure_store()
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = []
    return payload if isinstance(payload, list) else []


def _save_pages(items: list[dict[str, Any]]) -> None:
    _ensure_store()
    DATA_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _postgres_conn():
    return _pg_connection() if postgres_available() else None


def _mask_token(value: str) -> str:
    value = value or ""
    if len(value) <= 12:
        return "***" if value else ""
    return f"{value[:6]}...{value[-4:]}"


def _public_page(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_id": item.get("page_id", ""),
        "name": item.get("name", ""),
        "category": item.get("category", ""),
        "picture_url": item.get("picture_url", ""),
        "tasks": item.get("tasks") or [],
        "status": item.get("status", "connected"),
        "token_prefix": _mask_token(str(item.get("page_access_token") or "")),
        "connected_at": item.get("connected_at", ""),
        "updated_at": item.get("updated_at", ""),
        "expires_in": item.get("expires_in"),
    }


def list_facebook_pages() -> list[dict[str, Any]]:
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM facebook_pages ORDER BY updated_at DESC")
            items = [json.loads(row[0]) for row in cur.fetchall()]
        return [_public_page(item) for item in items]
    with STORE_LOCK:
        return [_public_page(item) for item in _load_pages()]


def _upsert_page(item: dict[str, Any]) -> dict[str, Any]:
    page_id = str(item["page_id"])
    item["updated_at"] = _now()
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO facebook_pages (page_id, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                ON CONFLICT (page_id) DO UPDATE SET
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (page_id, serialize_json(item)),
            )
        return item
    with STORE_LOCK:
        items = [page for page in _load_pages() if str(page.get("page_id")) != page_id]
        items.insert(0, item)
        _save_pages(items)
    return item


def connect_facebook_pages(short_lived_token: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.facebook_app_id or not settings.facebook_app_secret:
        raise RuntimeError("FACEBOOK_APP_ID and FACEBOOK_APP_SECRET are required.")
    short_lived_token = (short_lived_token or "").strip()
    if not short_lived_token:
        raise RuntimeError("Short-lived token is required.")

    base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    with httpx.Client(timeout=30) as client:
        exchange_response = client.get(
            f"{base_url}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.facebook_app_id,
                "client_secret": settings.facebook_app_secret,
                "fb_exchange_token": short_lived_token,
            },
        )
        exchange_response.raise_for_status()
        token_payload = exchange_response.json()
        long_lived_user_token = token_payload.get("access_token")
        if not long_lived_user_token:
            raise RuntimeError("Facebook did not return a long-lived user token.")

        pages_response = client.get(
            f"{base_url}/me/accounts",
            params={
                "fields": "id,name,category,access_token,tasks,picture{url}",
                "access_token": long_lived_user_token,
                "limit": 100,
            },
        )
        pages_response.raise_for_status()
        pages_payload = pages_response.json()

    connected_at = _now()
    batch_id = secrets.token_hex(8)
    pages: list[dict[str, Any]] = []
    for page in pages_payload.get("data") or []:
        page_id = str(page.get("id") or "").strip()
        page_token = str(page.get("access_token") or "").strip()
        if not page_id or not page_token:
            continue
        item = {
            "page_id": page_id,
            "name": page.get("name") or "",
            "category": page.get("category") or "",
            "picture_url": ((page.get("picture") or {}).get("data") or {}).get("url") or "",
            "tasks": page.get("tasks") or [],
            "page_access_token": page_token,
            "long_lived_user_token": long_lived_user_token,
            "user_token_prefix": _mask_token(long_lived_user_token),
            "expires_in": token_payload.get("expires_in"),
            "token_type": token_payload.get("token_type", "bearer"),
            "status": "connected",
            "connected_at": connected_at,
            "connect_batch_id": batch_id,
        }
        pages.append(_public_page(_upsert_page(item)))

    return {
        "status": "connected",
        "total": len(pages),
        "pages": pages,
        "batch_id": batch_id,
        "expires_in": token_payload.get("expires_in"),
    }
