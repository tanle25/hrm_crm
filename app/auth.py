from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.config import get_settings


def authenticate_credentials(username: str, password: str) -> bool:
    settings = get_settings()
    return hmac.compare_digest(username.strip(), settings.auth_username) and hmac.compare_digest(password, settings.auth_password)


def create_session_token(username: str, remember: bool = False) -> tuple[str, int]:
    settings = get_settings()
    max_age = 30 * 24 * 60 * 60 if remember else 12 * 60 * 60
    payload = {
        "sub": username.strip(),
        "iat": int(time.time()),
        "exp": int(time.time()) + max_age,
        "remember": bool(remember),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")
    signature = hmac.new(settings.auth_secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}", max_age


def verify_session_token(token: str | None) -> dict[str, Any] | None:
    settings = get_settings()
    if not token or "." not in token:
        return None
    payload_b64, signature = token.rsplit(".", 1)
    expected = hmac.new(settings.auth_secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(payload_json.decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return payload
