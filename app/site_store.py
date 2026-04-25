from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from app.postgres import get_connection as _pg_connection, postgres_available, serialize_json

DATA_PATH = Path("data/sites.json")
STORE_LOCK = Lock()


def _postgres_conn():
    return _pg_connection() if postgres_available() else None


def _ensure_store() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        DATA_PATH.write_text("[]", encoding="utf-8")


def _load_sites() -> list[dict[str, Any]]:
    _ensure_store()
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = []
    if not isinstance(payload, list):
        return []
    return [_migrate_legacy_keys(item) for item in payload if isinstance(item, dict)]


def _save_sites(items: list[dict[str, Any]]) -> None:
    _ensure_store()
    DATA_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _normalize_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    if not parsed.scheme:
        parsed = urllib.parse.urlparse(f"https://{value.strip()}")
    path = parsed.path.rstrip("/")
    normalized = parsed._replace(path=path, params="", query="", fragment="")
    return urllib.parse.urlunparse(normalized).rstrip("/")


def _migrate_legacy_keys(item: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(item)
    consumer_key = str(migrated.get("consumer_key") or "").strip()
    consumer_secret = str(migrated.get("consumer_secret") or "").strip()
    if not (consumer_key and consumer_secret):
        for key in migrated.get("api_keys") or []:
            name = str((key or {}).get("name") or "").strip().lower()
            value = str((key or {}).get("value") or "").strip()
            if not value:
                continue
            if name in {"consumer_key", "woo", "woo_key", "ck"} and not consumer_key:
                consumer_key = value
            elif name in {"consumer_secret", "woo_secret", "cs"} and not consumer_secret:
                consumer_secret = value
    migrated["consumer_key"] = consumer_key
    migrated["consumer_secret"] = consumer_secret
    migrated.pop("api_keys", None)
    return migrated


def list_sites(search: str | None = None) -> list[dict[str, Any]]:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM sites ORDER BY updated_at DESC")
            items = [_migrate_legacy_keys(json.loads(row[0])) for row in cur.fetchall()]
    else:
        with STORE_LOCK:
            items = _load_sites()
    search_lower = (search or "").strip().lower()
    if not search_lower:
        return sorted(items, key=lambda item: item.get("updated_at") or "", reverse=True)
    filtered: list[dict[str, Any]] = []
    for item in items:
        haystack = " ".join(
            [
                str(item.get("site_name") or ""),
                str(item.get("url") or ""),
                str(item.get("topic") or ""),
                str(item.get("username") or ""),
                str(item.get("consumer_key") or ""),
                str(item.get("consumer_secret") or ""),
            ]
        ).lower()
        if search_lower in haystack:
            filtered.append(item)
    return sorted(filtered, key=lambda item: item.get("updated_at") or "", reverse=True)


def get_site(site_id: str) -> dict[str, Any] | None:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("SELECT data::text FROM sites WHERE site_id = %s", (site_id,))
            row = cur.fetchone()
            return _migrate_legacy_keys(json.loads(row[0])) if row else None
    with STORE_LOCK:
        for item in _load_sites():
            if item.get("site_id") == site_id:
                return item
    return None


def create_site(payload: dict[str, Any]) -> dict[str, Any]:
    site = {
        "site_id": uuid4().hex,
        "url": _normalize_url(str(payload.get("url") or "")),
        "site_name": str(payload.get("site_name") or "").strip(),
        "topic": str(payload.get("topic") or "").strip(),
        "primary_color": str(payload.get("primary_color") or "#22c55e").strip() or "#22c55e",
        "consumer_key": str(payload.get("consumer_key") or "").strip(),
        "consumer_secret": str(payload.get("consumer_secret") or "").strip(),
        "username": str(payload.get("username") or "").strip(),
        "app_password": str(payload.get("app_password") or "").strip(),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "last_test_status": "untested",
        "last_test_message": "",
        "last_tested_at": "",
    }
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sites (site_id, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                """,
                (site["site_id"], serialize_json(site)),
            )
    else:
        with STORE_LOCK:
            items = _load_sites()
            items.append(site)
            _save_sites(items)
    return site


def update_site(site_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    current = get_site(site_id)
    if not current:
        return None
    updated = {
        **current,
        "url": _normalize_url(str(payload.get("url") or current.get("url") or "")),
        "site_name": str(payload.get("site_name") or current.get("site_name") or "").strip(),
        "topic": str(payload.get("topic") or current.get("topic") or "").strip(),
        "primary_color": str(payload.get("primary_color") or current.get("primary_color") or "#22c55e").strip() or "#22c55e",
        "consumer_key": str(payload.get("consumer_key") or current.get("consumer_key") or "").strip(),
        "consumer_secret": str(payload.get("consumer_secret") or current.get("consumer_secret") or "").strip(),
        "username": str(payload.get("username") or current.get("username") or "").strip(),
        "app_password": str(payload.get("app_password") or current.get("app_password") or "").strip(),
        "updated_at": _now_iso(),
    }
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE sites SET updated_at = NOW(), data = %s::jsonb WHERE site_id = %s",
                (serialize_json(updated), site_id),
            )
        return updated
    with STORE_LOCK:
        items = _load_sites()
        for index, item in enumerate(items):
            if item.get("site_id") != site_id:
                continue
            items[index] = updated
            _save_sites(items)
            return updated
    return None


def delete_site(site_id: str) -> bool:
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("DELETE FROM sites WHERE site_id = %s", (site_id,))
            return cur.rowcount > 0
    with STORE_LOCK:
        items = _load_sites()
        remaining = [item for item in items if item.get("site_id") != site_id]
        if len(remaining) == len(items):
            return False
        _save_sites(remaining)
    return True


def _wp_api_url(site_url: str) -> str:
    return f"{site_url.rstrip('/')}/wp-json/wp/v2/users/me?context=edit"


def _woo_products_url(site_url: str) -> str:
    return f"{site_url.rstrip('/')}/wp-json/wc/v3/products?per_page=1"


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def test_site_connection(site_id: str) -> dict[str, Any] | None:
    site = get_site(site_id)
    if not site:
        return None

    url = _wp_api_url(str(site.get("url") or ""))
    username = str(site.get("username") or "")
    app_password = str(site.get("app_password") or "")
    headers = {"Accept": "application/json"}
    if username and app_password:
        headers["Authorization"] = _basic_auth_header(username, app_password)

    status = "offline"
    message = "Không thể kết nối website."
    details: dict[str, Any] = {}

    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="ignore")
            payload = json.loads(body) if body else {}
            status = "connected"
            message = "Kết nối WordPress REST API thành công."
            details = {
                "http_status": response.status,
                "user": payload.get("name") or payload.get("slug") or "",
            }
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="ignore")
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        if error.code in {401, 403}:
            status = "unauthorized"
            message = payload.get("message") or "Website phản hồi nhưng tài khoản hoặc app password chưa đúng."
        else:
            status = "error"
            message = payload.get("message") or f"Website trả HTTP {error.code}."
        details = {"http_status": error.code}
    except Exception as error:  # pragma: no cover
        status = "offline"
        message = str(error)

    consumer_key = str(site.get("consumer_key") or "").strip()
    consumer_secret = str(site.get("consumer_secret") or "").strip()
    if consumer_key and consumer_secret:
        woo_status = "offline"
        woo_message = "Không thể kết nối WooCommerce REST API."
        try:
            woo_request = urllib.request.Request(
                _woo_products_url(str(site.get("url") or "")),
                headers={
                    "Accept": "application/json",
                    "Authorization": _basic_auth_header(consumer_key, consumer_secret),
                },
                method="GET",
            )
            with urllib.request.urlopen(woo_request, timeout=15) as response:
                woo_status = "connected"
                woo_message = "Kết nối WooCommerce REST API thành công."
                details["woo_http_status"] = response.status
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="ignore")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {}
            woo_status = "unauthorized" if error.code in {401, 403} else "error"
            woo_message = payload.get("message") or f"WooCommerce trả HTTP {error.code}."
            details["woo_http_status"] = error.code
        except Exception as error:  # pragma: no cover
            woo_status = "offline"
            woo_message = str(error)

        details["woo_status"] = woo_status
        details["woo_message"] = woo_message
        if woo_status != "connected":
            status = woo_status
            message = woo_message

    updated_site = {
        **site,
        "last_test_status": status,
        "last_test_message": message,
        "last_tested_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    pg_conn = _postgres_conn()
    if pg_conn is not None:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE sites SET updated_at = NOW(), data = %s::jsonb WHERE site_id = %s",
                (serialize_json(updated_site), site_id),
            )
    else:
        with STORE_LOCK:
            items = _load_sites()
            for index, item in enumerate(items):
                if item.get("site_id") != site_id:
                    continue
                items[index] = updated_site
                break
            _save_sites(items)

    return {
        "site_id": site_id,
        "status": status,
        "message": message,
        "details": details,
    }
