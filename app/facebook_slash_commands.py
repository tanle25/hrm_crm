from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from app.postgres import get_connection as _pg_connection, postgres_available, serialize_json


DATA_PATH = Path("data/facebook_slash_commands.json")
STORE_LOCK = Lock()

DEFAULT_FACEBOOK_SLASH_COMMANDS = [
    {"command": "/gia", "label": "Hỏi nhu cầu / báo giá", "text": "Anh/chị muốn em gửi báo giá mẫu nào ạ?"},
    {"command": "/ship", "label": "Giao hàng", "text": "Bên em có giao hàng toàn quốc, anh/chị nhận hàng kiểm tra rồi thanh toán ạ."},
    {"command": "/zalo", "label": "Xin Zalo/SĐT", "text": "Anh/chị cho em xin số Zalo để em gửi hình và tư vấn nhanh hơn nhé."},
    {"command": "/camon", "label": "Cảm ơn", "text": "Em cảm ơn anh/chị đã quan tâm. Anh/chị cần thêm hình/video mẫu nào em gửi ngay ạ."},
    {"command": "/chot", "label": "Chốt đơn", "text": "Nếu anh/chị chốt mẫu này, anh/chị gửi giúp em tên, số điện thoại và địa chỉ nhận hàng nhé."},
]


def _postgres_conn():
    return _pg_connection() if postgres_available() else None


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _normalize_command(command: str) -> str:
    normalized = " ".join(str(command or "").strip().split()).lower()
    if not normalized:
        raise ValueError("Command is required.")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if len(normalized) > 40:
        raise ValueError("Command is too long.")
    return normalized


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    command = _normalize_command(str(item.get("command") or ""))
    label = str(item.get("label") or "").strip()
    text = str(item.get("text") or "").strip()
    if not label:
        raise ValueError("Label is required.")
    if not text:
        raise ValueError("Text is required.")
    return {
        "command": command,
        "label": label[:120],
        "text": text[:2000],
        "updated_at": _now_iso(),
    }


def _ensure_store() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        DATA_PATH.write_text(json.dumps(DEFAULT_FACEBOOK_SLASH_COMMANDS, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_file_commands() -> list[dict[str, Any]]:
    _ensure_store()
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _save_file_commands(items: list[dict[str, Any]]) -> None:
    _ensure_store()
    DATA_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_postgres_defaults_if_empty() -> None:
    pg_conn = _postgres_conn()
    if pg_conn is None:
        return
    with pg_conn, pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM job_meta WHERE key = 'facebook_slash_commands_seeded'")
        if cur.fetchone():
            return
        for item in DEFAULT_FACEBOOK_SLASH_COMMANDS:
            cur.execute(
                """
                INSERT INTO facebook_slash_commands (command, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                ON CONFLICT (command) DO NOTHING
                """,
                (item["command"], serialize_json({**item, "updated_at": _now_iso()})),
            )
        cur.execute(
            """
            INSERT INTO job_meta (key, value)
            VALUES ('facebook_slash_commands_seeded', 1)
            ON CONFLICT (key) DO UPDATE SET value = 1
            """
        )


def list_facebook_slash_commands() -> list[dict[str, Any]]:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        _seed_postgres_defaults_if_empty()
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM facebook_slash_commands ORDER BY command ASC")
            return [json.loads(row[0]) for row in cur.fetchall()]
    with STORE_LOCK:
        return sorted(_load_file_commands(), key=lambda item: str(item.get("command") or ""))


def upsert_facebook_slash_command(item: dict[str, Any], original_command: str = "") -> dict[str, Any]:
    normalized = _normalize_item(item)
    original = _normalize_command(original_command) if original_command else normalized["command"]
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            if original != normalized["command"]:
                cur.execute("DELETE FROM facebook_slash_commands WHERE command = %s", (original,))
            cur.execute(
                """
                INSERT INTO facebook_slash_commands (command, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                ON CONFLICT (command) DO UPDATE SET updated_at = NOW(), data = EXCLUDED.data
                """,
                (normalized["command"], serialize_json(normalized)),
            )
        return normalized
    with STORE_LOCK:
        items = [
            entry for entry in _load_file_commands()
            if str(entry.get("command") or "") not in {original, normalized["command"]}
        ]
        items.append(normalized)
        _save_file_commands(sorted(items, key=lambda entry: str(entry.get("command") or "")))
    return normalized


def delete_facebook_slash_command(command: str) -> bool:
    normalized = _normalize_command(command)
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("DELETE FROM facebook_slash_commands WHERE command = %s", (normalized,))
            return cur.rowcount > 0
    with STORE_LOCK:
        items = _load_file_commands()
        remaining = [item for item in items if str(item.get("command") or "") != normalized]
        if len(remaining) == len(items):
            return False
        _save_file_commands(remaining)
    return True
