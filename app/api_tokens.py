from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from app.postgres import get_connection as _pg_connection, postgres_available, serialize_json

DATA_PATH = Path("data/api_tokens.json")
STORE_LOCK = Lock()


def _postgres_conn():
    return _pg_connection() if postgres_available() else None


def _ensure_store() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        DATA_PATH.write_text("[]", encoding="utf-8")


def _load_tokens() -> list[dict[str, Any]]:
    _ensure_store()
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _save_tokens(items: list[dict[str, Any]]) -> None:
    _ensure_store()
    DATA_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def list_api_tokens() -> list[dict[str, Any]]:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM api_tokens ORDER BY updated_at DESC")
            return [json.loads(row[0]) for row in cur.fetchall()]
    with STORE_LOCK:
        return _load_tokens()


def create_api_token(name: str) -> tuple[dict[str, Any], str]:
    token_id = uuid4().hex
    raw_token = f"cf_ext_{secrets.token_urlsafe(32)}"
    item = {
        "token_id": token_id,
        "name": name.strip() or "Extension Token",
        "token_prefix": raw_token[:16],
        "token_hash": _token_hash(raw_token),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "last_used_at": "",
        "status": "active",
    }
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_tokens (token_id, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                """,
                (token_id, serialize_json(item)),
            )
    else:
        with STORE_LOCK:
            items = _load_tokens()
            items.append(item)
            _save_tokens(items)
    return item, raw_token


def delete_api_token(token_id: str) -> bool:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("DELETE FROM api_tokens WHERE token_id = %s", (token_id,))
            return cur.rowcount > 0
    with STORE_LOCK:
        items = _load_tokens()
        remaining = [item for item in items if item.get("token_id") != token_id]
        if len(remaining) == len(items):
            return False
        _save_tokens(remaining)
        return True


def verify_api_token(raw_token: str | None) -> dict[str, Any] | None:
    if not raw_token:
        return None
    hashed = _token_hash(raw_token.strip())
    items = list_api_tokens()
    for item in items:
        if item.get("status") != "active":
            continue
        if secrets.compare_digest(str(item.get("token_hash") or ""), hashed):
            touch_api_token(str(item.get("token_id") or ""))
            return item
    return None


def touch_api_token(token_id: str) -> None:
    if not token_id:
        return
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM api_tokens WHERE token_id = %s", (token_id,))
            row = cur.fetchone()
            if not row:
                return
            item = json.loads(row[0])
            item["last_used_at"] = _now_iso()
            item["updated_at"] = _now_iso()
            cur.execute(
                "UPDATE api_tokens SET updated_at = NOW(), data = %s::jsonb WHERE token_id = %s",
                (serialize_json(item), token_id),
            )
        return
    with STORE_LOCK:
        items = _load_tokens()
        changed = False
        for item in items:
            if item.get("token_id") != token_id:
                continue
            item["last_used_at"] = _now_iso()
            item["updated_at"] = _now_iso()
            changed = True
            break
        if changed:
            _save_tokens(items)
