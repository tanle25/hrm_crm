from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query


AUTH_DIR = Path(os.getenv("CLIPROXY_AUTH_DIR", "~/.cli-proxy-api")).expanduser()
CACHE_TTL = max(10, int(os.getenv("CACHE_TTL", "60")))
HOST = os.getenv("QUOTA_HOST", "127.0.0.1")
PORT = int(os.getenv("QUOTA_PORT", "8320"))
REQUEST_TIMEOUT = float(os.getenv("QUOTA_REQUEST_TIMEOUT", "20"))
WHAM_USAGE_URL = os.getenv("WHAM_USAGE_URL", "https://chatgpt.com/backend-api/wham/usage")

app = FastAPI(title="CLIProxy Codex Quota Aggregator", version="1.0.0")

_cache_lock = asyncio.Lock()
_quota_cache: dict[str, Any] | None = None
_quota_cache_time = 0.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _human_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return ""
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, _ = divmod(remaining, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception:
        return {}


def _id_token_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            pass
        return _decode_jwt_payload(stripped)
    return {}


def _auth_files() -> list[Path]:
    if not AUTH_DIR.exists():
        return []
    return sorted(path for path in AUTH_DIR.glob("codex-*.json") if path.is_file())


def _load_auth_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "file": path.name,
            "path": str(path),
            "status": "invalid_json",
            "error": str(exc),
            "access_token": "",
        }
    if not isinstance(data, dict):
        return {
            "file": path.name,
            "path": str(path),
            "status": "invalid_json",
            "error": "Auth file is not a JSON object.",
            "access_token": "",
        }

    id_token = _id_token_payload(data.get("id_token"))
    subscription_until = id_token.get("chatgpt_subscription_active_until") or data.get("subscription_until")
    subscription_until_dt = _parse_datetime(subscription_until)
    token_expires_at = _parse_datetime(data.get("expired") or data.get("expires_at"))
    now = _utc_now()
    return {
        "file": path.name,
        "path": str(path),
        "email": data.get("email") or id_token.get("email") or data.get("account") or path.stem,
        "account_id": data.get("account_id") or id_token.get("chatgpt_account_id") or "",
        "plan": id_token.get("plan_type") or data.get("plan_type") or "",
        "subscription_until": subscription_until or "",
        "subscription_expired": bool(subscription_until_dt and subscription_until_dt <= now),
        "disabled": bool(data.get("disabled")),
        "token_expired": bool(token_expires_at and token_expires_at <= now),
        "token_expires_at": data.get("expired") or data.get("expires_at") or "",
        "access_token": str(data.get("access_token") or ""),
    }


def _account_public(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": account.get("file", ""),
        "email": account.get("email", ""),
        "plan": account.get("plan", ""),
        "subscription_until": account.get("subscription_until", ""),
        "expired": bool(account.get("subscription_expired")),
        "disabled": bool(account.get("disabled")),
        "token_expired": bool(account.get("token_expired")),
    }


def list_accounts() -> list[dict[str, Any]]:
    return [_account_public(_load_auth_file(path)) for path in _auth_files()]


def _window_payload(window: dict[str, Any] | None) -> dict[str, Any]:
    window = window or {}
    used_percent = max(0, min(100, int(window.get("used_percent") or 0)))
    reset_after = window.get("reset_after_seconds")
    reset_at = window.get("reset_at")
    reset_at_iso = ""
    if reset_at:
        try:
            reset_at_iso = datetime.fromtimestamp(int(reset_at), tz=timezone.utc).isoformat()
        except Exception:
            reset_at_iso = ""
    return {
        "used_percent": used_percent,
        "remaining_percent": 100 - used_percent,
        "reset_at": reset_at_iso,
        "reset_after_human": _human_duration(reset_after),
        "limit_window_seconds": int(window.get("limit_window_seconds") or 0),
    }


async def _fetch_account_usage(client: httpx.AsyncClient, account: dict[str, Any]) -> dict[str, Any]:
    base = {
        "file": account.get("file", ""),
        "account": account.get("email", ""),
        "plan": account.get("plan", ""),
        "subscription_until": account.get("subscription_until", ""),
        "status": "unknown",
        "limit_reached": False,
        "five_hour": _window_payload({}),
        "weekly": _window_payload({}),
        "credits": {"has_credits": False, "balance": "0"},
    }

    if account.get("status") == "invalid_json":
        return {**base, "status": "invalid_json", "error": account.get("error", "")}
    if account.get("disabled"):
        return {**base, "status": "disabled"}
    if account.get("subscription_expired"):
        return {**base, "status": "subscription_expired"}
    if account.get("token_expired"):
        return {**base, "status": "token_expired"}
    token = str(account.get("access_token") or "")
    if not token:
        return {**base, "status": "missing_token"}

    try:
        response = await client.get(
            WHAM_USAGE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    except Exception as exc:
        return {**base, "status": "exception", "error": str(exc)}

    if response.status_code == 401:
        return {**base, "status": "unauthorized"}
    if response.status_code != 200:
        return {**base, "status": f"http_{response.status_code}", "error": response.text[:500]}

    try:
        data = response.json()
    except Exception as exc:
        return {**base, "status": "invalid_response", "error": str(exc)}

    rate_limit = data.get("rate_limit") or {}
    credits = data.get("credits") or {}
    plan_type = str(data.get("plan_type") or base["plan"] or "").strip().lower()
    status = "subscription_expired" if plan_type == "free" else "ok"
    return {
        **base,
        "account": data.get("email") or base["account"],
        "plan": plan_type or base["plan"],
        "status": status,
        "limit_reached": bool(rate_limit.get("limit_reached")),
        "five_hour": _window_payload(rate_limit.get("primary_window")),
        "weekly": _window_payload(rate_limit.get("secondary_window")),
        "credits": {
            "has_credits": bool(credits.get("has_credits")),
            "balance": str(credits.get("balance") or "0"),
        },
    }


def _summary(accounts: list[dict[str, Any]], fetched_at: str, cache_age_sec: int) -> dict[str, Any]:
    healthy = [item for item in accounts if item.get("status") == "ok" and not item.get("limit_reached")]
    expired = [item for item in accounts if item.get("status") == "subscription_expired"]
    limit_reached = [item for item in accounts if item.get("limit_reached")]
    errored = [
        item
        for item in accounts
        if item.get("status") not in {"ok", "subscription_expired", "disabled"}
    ]

    rem_5h = [int(item.get("five_hour", {}).get("remaining_percent") or 0) for item in healthy]
    rem_7d = [int(item.get("weekly", {}).get("remaining_percent") or 0) for item in healthy]

    avg_5h = round(sum(rem_5h) / len(rem_5h), 2) if rem_5h else 0
    avg_7d = round(sum(rem_7d) / len(rem_7d), 2) if rem_7d else 0
    min_5h = min(rem_5h) if rem_5h else 0
    min_7d = min(rem_7d) if rem_7d else 0
    max_5h = max(rem_5h) if rem_5h else 0
    max_7d = max(rem_7d) if rem_7d else 0
    stale = cache_age_sec > CACHE_TTL * 2
    safe_to_use = bool(healthy and avg_5h >= 10 and min_7d >= 5 and not stale)

    return {
        "total": len(accounts),
        "healthy": len(healthy),
        "expired": len(expired),
        "limit_reached": len(limit_reached),
        "errored": len(errored),
        "avg_remaining_5h": avg_5h,
        "avg_remaining_7d": avg_7d,
        "min_remaining_5h": min_5h,
        "min_remaining_7d": min_7d,
        "max_remaining_5h": max_5h,
        "max_remaining_7d": max_7d,
        "safe_to_use": safe_to_use,
        "stale": stale,
        "cache_age_sec": cache_age_sec,
        "fetched_at": fetched_at,
    }


async def collect_quota() -> dict[str, Any]:
    accounts = [_load_auth_file(path) for path in _auth_files()]
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        results = await asyncio.gather(*[_fetch_account_usage(client, account) for account in accounts])
    fetched_at = _iso_now()
    return {
        "summary": _summary(results, fetched_at, 0),
        "accounts": results,
        "fetched_at": fetched_at,
    }


async def get_quota(force: bool = False) -> dict[str, Any]:
    global _quota_cache, _quota_cache_time
    async with _cache_lock:
        now = time.monotonic()
        if not force and _quota_cache is not None and now - _quota_cache_time <= CACHE_TTL:
            data = dict(_quota_cache)
            data["summary"] = _summary(data.get("accounts", []), data.get("fetched_at", ""), int(now - _quota_cache_time))
            return data
        _quota_cache = await collect_quota()
        _quota_cache_time = time.monotonic()
        return _quota_cache


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "auth_dir": str(AUTH_DIR), "files_found": len(_auth_files())}


@app.get("/accounts")
async def accounts() -> list[dict[str, Any]]:
    return list_accounts()


@app.get("/quota")
async def quota(force: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    return await get_quota(force=bool(force))


@app.get("/quota/summary")
async def quota_summary(force: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    data = await get_quota(force=bool(force))
    return data["summary"]


@app.get("/quota/best")
async def quota_best(force: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    data = await get_quota(force=bool(force))
    candidates = [
        item
        for item in data.get("accounts", [])
        if item.get("status") == "ok" and not item.get("limit_reached")
    ]
    if not candidates:
        raise HTTPException(status_code=503, detail="No healthy Codex account available.")
    best = max(
        candidates,
        key=lambda item: min(
            int(item.get("five_hour", {}).get("remaining_percent") or 0),
            int(item.get("weekly", {}).get("remaining_percent") or 0),
        ),
    )
    return {
        "account": best.get("account", ""),
        "file": best.get("file", ""),
        "five_hour_remaining": best.get("five_hour", {}).get("remaining_percent", 0),
        "weekly_remaining": best.get("weekly", {}).get("remaining_percent", 0),
        "five_hour_resets_in": best.get("five_hour", {}).get("reset_after_human", ""),
        "weekly_resets_in": best.get("weekly", {}).get("reset_after_human", ""),
    }


@app.get("/quota/{account_id}")
async def quota_account(account_id: str, force: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    data = await get_quota(force=bool(force))
    needle = account_id.strip().lower()
    for account in data.get("accounts", []):
        candidates = [
            str(account.get("account") or ""),
            str(account.get("file") or ""),
        ]
        if any(needle == value.lower() or needle in value.lower() for value in candidates):
            return account
    raise HTTPException(status_code=404, detail="Account not found.")


if __name__ == "__main__":
    print(f"Auth dir: {AUTH_DIR}")
    print(f"Codex accounts found: {len(_auth_files())}")
    print(f"Dashboard: http://{HOST}:{PORT}/quota")
    uvicorn.run(app, host=HOST, port=PORT)
