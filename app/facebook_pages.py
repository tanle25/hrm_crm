from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

from app.config import get_settings
from app.job_store import publish_realtime_event
from app.postgres import get_connection as _pg_connection, postgres_available, serialize_json

try:
    from redis import Redis
    from rq import Queue
except ImportError:  # pragma: no cover
    Redis = None
    Queue = None


DATA_PATH = Path("data/facebook_pages.json")
GROUPS_PATH = Path("data/facebook_page_groups.json")
SYNC_JOBS_PATH = Path("data/facebook_sync_jobs")
STORE_LOCK = Lock()
MESSAGE_DETAIL_FIELDS = "id,created_time,from,to,message,attachments,shares,sticker,reply_to{id,message,created_time,from,attachments,shares,sticker}"
POST_ANALYTICS_KEYS = ["reach", "impressions", "engagement", "clicks", "comments", "reactions", "shares"]
POST_INSIGHT_TARGETS = {
    "reach": ["post_impressions_unique"],
    "impressions": ["post_impressions"],
    "engagement": ["post_engaged_users"],
    "clicks": ["post_clicks", "post_clicks_by_type"],
}


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


def _load_page_groups() -> list[str]:
    GROUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not GROUPS_PATH.exists():
        return []
    try:
        payload = json.loads(GROUPS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return sorted({" ".join(str(item or "").strip().split())[:80] for item in payload if str(item or "").strip()})


def _save_page_groups(groups: list[str]) -> None:
    GROUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    cleaned = sorted({" ".join(str(item or "").strip().split())[:80] for item in groups if str(item or "").strip()})
    GROUPS_PATH.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")


def _postgres_conn():
    return _pg_connection() if postgres_available() else None


def _upsert_facebook_sync_job(job: dict[str, Any]) -> None:
    job["updated_at"] = _now()
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO facebook_sync_jobs (job_id, status, updated_at, data)
                VALUES (%s, %s, NOW(), %s::jsonb)
                ON CONFLICT (job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    updated_at = NOW(),
                    data = EXCLUDED.data
                """,
                (str(job.get("job_id") or ""), str(job.get("status") or "queued"), serialize_json(job)),
            )
        return
    SYNC_JOBS_PATH.mkdir(parents=True, exist_ok=True)
    (SYNC_JOBS_PATH / f"{job.get('job_id')}.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def get_facebook_sync_job(job_id: str) -> dict[str, Any] | None:
    job_id = (job_id or "").strip()
    if not job_id:
        return None
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM facebook_sync_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
    path = SYNC_JOBS_PATH / f"{job_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def latest_facebook_sync_jobs(limit: int = 5) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 20))
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM facebook_sync_jobs ORDER BY updated_at DESC LIMIT %s", (limit,))
            return [json.loads(row[0]) for row in cur.fetchall()]
    if not SYNC_JOBS_PATH.exists():
        return []
    paths = sorted(SYNC_JOBS_PATH.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def _rq_content_queue():
    settings = get_settings()
    if Redis is None or Queue is None:
        return None
    try:
        redis_conn = Redis.from_url(settings.redis_url)
        redis_conn.ping()
        return Queue("content_pipeline", connection=redis_conn)
    except Exception:
        return None


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
        "cover_url": item.get("cover_url", ""),
        "group": item.get("group", ""),
        "tasks": item.get("tasks") or [],
        "status": item.get("status", "connected"),
        "token_prefix": _mask_token(str(item.get("page_access_token") or "")),
        "connected_at": item.get("connected_at", ""),
        "updated_at": item.get("updated_at", ""),
        "expires_in": item.get("expires_in"),
        "webhook_subscribed": bool(item.get("webhook_subscribed")),
        "webhook_subscribed_at": item.get("webhook_subscribed_at", ""),
        "webhook_subscribe_error": item.get("webhook_subscribe_error", ""),
    }


def list_facebook_pages() -> list[dict[str, Any]]:
    return [_public_page(item) for item in _list_facebook_page_records()]


def list_facebook_page_groups() -> list[dict[str, Any]]:
    pages = _list_facebook_page_records()
    counts: defaultdict[str, int] = defaultdict(int)
    for page in pages:
        group = " ".join(str(page.get("group") or "").strip().split())[:80]
        if group:
            counts[group] += 1
    group_names = set(counts.keys())
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT group_name FROM facebook_page_groups ORDER BY group_name ASC")
            group_names.update(str(row[0] or "") for row in cur.fetchall())
    else:
        group_names.update(_load_page_groups())
    return [{"name": name, "page_count": counts.get(name, 0)} for name in sorted(name for name in group_names if name)]


def create_facebook_page_group(group: str) -> dict[str, Any]:
    group = " ".join(str(group or "").strip().split())[:80]
    if not group:
        raise RuntimeError("Group name is required.")
    payload = {"name": group, "created_at": _now(), "updated_at": _now()}
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO facebook_page_groups (group_name, updated_at, data)
                VALUES (%s, NOW(), %s::jsonb)
                ON CONFLICT (group_name) DO UPDATE SET updated_at = NOW(), data = EXCLUDED.data
                """,
                (group, serialize_json(payload)),
            )
    else:
        with STORE_LOCK:
            groups = _load_page_groups()
            if group not in groups:
                groups.append(group)
            _save_page_groups(groups)
    return {"name": group, "page_count": 0}


def update_facebook_page_group(page_id: str, group: str) -> dict[str, Any]:
    page_id = str(page_id or "").strip()
    if not page_id:
        raise RuntimeError("page_id is required.")
    group = " ".join(str(group or "").strip().split())[:80]
    if group:
        create_facebook_page_group(group)
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM facebook_pages WHERE page_id = %s", (page_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Facebook page not found.")
            item = json.loads(row[0])
            item["group"] = group
            item["updated_at"] = _now()
            cur.execute(
                "UPDATE facebook_pages SET updated_at = NOW(), data = %s::jsonb WHERE page_id = %s",
                (serialize_json(item), page_id),
            )
            return _public_page(item)
    with STORE_LOCK:
        items = _load_pages()
        for item in items:
            if str(item.get("page_id") or "") != page_id:
                continue
            item["group"] = group
            item["updated_at"] = _now()
            _save_pages(items)
            return _public_page(item)
    raise RuntimeError("Facebook page not found.")


def _list_facebook_page_records() -> list[dict[str, Any]]:
    conn = _postgres_conn()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data::text FROM facebook_pages ORDER BY updated_at DESC")
            return [json.loads(row[0]) for row in cur.fetchall()]
    with STORE_LOCK:
        return _load_pages()


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


def _subscribe_page_webhook(client: httpx.Client, base_url: str, page_id: str, page_token: str) -> dict[str, Any]:
    fields = "messages,message_echoes,messaging_postbacks"
    try:
        response = client.post(
            f"{base_url}/{page_id}/subscribed_apps",
            data={
                "subscribed_fields": fields,
                "access_token": page_token,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return {
            "webhook_subscribed": bool(payload.get("success", True)),
            "webhook_subscribed_at": _now(),
            "webhook_subscribed_fields": fields.split(","),
            "webhook_subscribe_error": "",
        }
    except httpx.HTTPError as error:
        return {
            "webhook_subscribed": False,
            "webhook_subscribed_at": "",
            "webhook_subscribed_fields": fields.split(","),
            "webhook_subscribe_error": _graph_error_message(error, "Page webhook subscribe failed"),
        }


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
                "fields": "id,name,category,access_token,tasks,picture.width(256).height(256){url},cover{source}",
                "access_token": long_lived_user_token,
                "limit": 100,
            },
        )
        pages_response.raise_for_status()
        pages_payload = pages_response.json()

    connected_at = _now()
    batch_id = secrets.token_hex(8)
    pages: list[dict[str, Any]] = []
    with httpx.Client(timeout=30) as client:
        for page in pages_payload.get("data") or []:
            page_id = str(page.get("id") or "").strip()
            page_token = str(page.get("access_token") or "").strip()
            if not page_id or not page_token:
                continue
            webhook_status = _subscribe_page_webhook(client, base_url, page_id, page_token)
            item = {
                "page_id": page_id,
                "name": page.get("name") or "",
                "category": page.get("category") or "",
                "picture_url": ((page.get("picture") or {}).get("data") or {}).get("url") or "",
                "cover_url": (page.get("cover") or {}).get("source") or "",
                "tasks": page.get("tasks") or [],
                "page_access_token": page_token,
                "long_lived_user_token": long_lived_user_token,
                "user_token_prefix": _mask_token(long_lived_user_token),
                "expires_in": token_payload.get("expires_in"),
                "token_type": token_payload.get("token_type", "bearer"),
                "status": "connected",
                "connected_at": connected_at,
                "connect_batch_id": batch_id,
                **webhook_status,
            }
            pages.append(_public_page(_upsert_page(item)))

    return {
        "status": "connected",
        "total": len(pages),
        "pages": pages,
        "batch_id": batch_id,
        "expires_in": token_payload.get("expires_in"),
    }


def _insight_values(payload: dict[str, Any], metric: str) -> list[dict[str, Any]]:
    for item in payload.get("data") or []:
        if item.get("name") == metric:
            return item.get("values") or []
    return []


def _graph_error_message(error: httpx.HTTPError, fallback: str = "Graph API request failed") -> str:
    response = getattr(error, "response", None)
    if response is None:
        return fallback
    try:
        payload = response.json()
    except ValueError:
        return f"{fallback}: HTTP {response.status_code}"
    graph_error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(graph_error, dict):
        message = graph_error.get("message") or fallback
        code = graph_error.get("code")
        subcode = graph_error.get("error_subcode")
        suffix = f"code={code}" if code else ""
        if subcode:
            suffix = f"{suffix}, subcode={subcode}" if suffix else f"subcode={subcode}"
        return f"{message} ({suffix})" if suffix else str(message)
    return f"{fallback}: HTTP {response.status_code}"


def _graph_error_kind(message: str) -> str:
    lowered = (message or "").lower()
    if "permission" in lowered or "permissions" in lowered or "cannot" in lowered or "not authorized" in lowered:
        return "permission_denied"
    if "deprecated" in lowered or "valid insights metric" in lowered or "unsupported" in lowered:
        return "unsupported"
    if "rate" in lowered or "too many" in lowered:
        return "rate_limited"
    return "api_error"


def _fetch_metrics_payload(
    client: httpx.Client,
    base_url: str,
    page: dict[str, Any],
    metrics: list[str],
    since: datetime,
    until: datetime,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    values_by_metric: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for metric in metrics:
        try:
            response = client.get(
                f"{base_url}/{page['page_id']}/insights",
                params={
                    "metric": metric,
                    "period": "day",
                    "since": int(since.timestamp()),
                    "until": int(until.timestamp()),
                    "access_token": page["page_access_token"],
                },
            )
            response.raise_for_status()
            values_by_metric[metric] = _insight_values(response.json(), metric)
        except httpx.HTTPError as error:
            warnings.append(f"{page.get('name') or page.get('page_id')}: {metric} unavailable - {_graph_error_message(error)}")
    return values_by_metric, warnings


class FacebookGraphClient:
    def __init__(self, client: httpx.Client, base_url: str, token: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.token = token

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(params or {})
        payload["access_token"] = self.token
        response = self.client.get(f"{self.base_url}/{path.lstrip('/')}", params=payload)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def fetch_post_page(self, page_id: str, *, limit: int, after: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "fields": "id,message,created_time,permalink_url",
            "limit": max(1, min(limit, 25)),
        }
        if after:
            params["after"] = after
        return self.get_json(f"{page_id}/posts", params)

    def fetch_post_analytics(self, post_id: str) -> tuple[dict[str, int], dict[str, str]]:
        metrics = {key: 0 for key in POST_ANALYTICS_KEYS}
        errors: dict[str, str] = {}
        insight_ids = [post_id]
        object_id = post_id.split("_", 1)[1] if "_" in post_id else ""
        if object_id:
            insight_ids.append(object_id)

        for target, candidates in POST_INSIGHT_TARGETS.items():
            last_error = ""
            found = False
            for graph_id in insight_ids:
                for metric_name in candidates:
                    try:
                        value = _post_insight_total(self.get_json(f"{graph_id}/insights", {"metric": metric_name}), metric_name)
                        if value:
                            metrics[target] = value
                            found = True
                            break
                    except httpx.HTTPError as error:
                        last_error = _graph_error_message(error)
                if found:
                    break
            if not found and last_error:
                errors[target] = last_error

        for target, edge in [("comments", "comments"), ("reactions", "reactions")]:
            last_error = ""
            for graph_id in insight_ids:
                try:
                    payload = self.get_json(f"{graph_id}/{edge}", {"summary": "true", "limit": 0})
                    metrics[target] = _safe_int(((payload.get("summary") or {}).get("total_count")))
                    last_error = ""
                    break
                except httpx.HTTPError as error:
                    last_error = _graph_error_message(error)
            if last_error:
                errors[target] = last_error

        last_error = ""
        for graph_id in insight_ids:
            try:
                payload = self.get_json(graph_id, {"fields": "shares"})
                metrics["shares"] = _safe_int(((payload.get("shares") or {}).get("count")))
                last_error = ""
                break
            except httpx.HTTPError as error:
                last_error = _graph_error_message(error)
        if last_error:
            errors["shares"] = last_error

        return metrics, errors


def _safe_int(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_safe_int(item) for item in value.values())
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _date_key(value: str | None) -> str:
    if not value:
        return ""
    return value[:10]


def _post_insight_total(insights: dict[str, Any] | None, metric: str) -> int:
    if not insights:
        return 0
    values = _insight_values(insights, metric)
    return sum(_safe_int(item.get("value")) for item in values)


def _parse_graph_time(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value)
    normalized = raw.replace("Z", "+00:00")
    if len(normalized) >= 5 and normalized[-5] in ["+", "-"] and normalized[-3] != ":":
        normalized = f"{normalized[:-2]}:{normalized[-2:]}"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _parse_graph_time_for_db(value: str | None) -> datetime | None:
    parsed = _parse_graph_time(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _comment_sentiment(message: str) -> str:
    text = (message or "").lower()
    if any(term in text for term in ["lỗi", "tệ", "chậm", "không trả lời", "hoàn tiền", "bực", "kém", "xấu", "lừa", "scam"]):
        return "negative"
    if "?" in text or any(term in text for term in ["giá", "bao nhiêu", "còn không", "mua", "ở đâu", "ship", "tư vấn"]):
        return "question"
    if any(term in text for term in ["hay", "tốt", "cảm ơn", "ok", "đẹp", "thích", "tuyệt"]):
        return "positive"
    return "neutral"


def _normalize_facebook_comment(
    comment: dict[str, Any],
    *,
    page: dict[str, Any],
    post_id: str,
    post_message: str,
    post_link: str,
) -> dict[str, Any]:
    message = str(comment.get("message") or "")
    sentiment = _comment_sentiment(message)
    author = comment.get("from") or {}
    return {
        "comment_id": str(comment.get("id") or ""),
        "post_id": post_id,
        "page_id": str(page.get("page_id") or ""),
        "page_name": page.get("name") or "",
        "page_picture_url": page.get("picture_url") or "",
        "author_id": str(author.get("id") or ""),
        "author_name": author.get("name") or "Facebook User",
        "message": message,
        "created_time": str(comment.get("created_time") or ""),
        "post_message": post_message,
        "permalink_url": post_link,
        "like_count": _safe_int(comment.get("like_count")),
        "reply_count": _safe_int(comment.get("comment_count")),
        "sentiment": sentiment,
        "status": "auto" if sentiment == "positive" else "pending",
    }


def _upsert_facebook_post(item: dict[str, Any]) -> None:
    conn = _postgres_conn()
    if conn is None:
        return
    post_id = str(item.get("post_id") or "")
    if not post_id:
        return
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facebook_posts (post_id, page_id, created_time, updated_at, data)
            VALUES (%s, %s, %s, NOW(), %s::jsonb)
            ON CONFLICT (post_id) DO UPDATE SET
                page_id = EXCLUDED.page_id,
                created_time = EXCLUDED.created_time,
                updated_at = NOW(),
                data = EXCLUDED.data
            """,
            (
                post_id,
                str(item.get("page_id") or ""),
                _parse_graph_time_for_db(str(item.get("created_time") or "")),
                serialize_json(item),
            ),
        )


def _upsert_facebook_comment(item: dict[str, Any]) -> None:
    conn = _postgres_conn()
    if conn is None:
        return
    comment_id = str(item.get("comment_id") or "")
    if not comment_id:
        return
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facebook_comments (comment_id, post_id, page_id, created_time, updated_at, data)
            VALUES (%s, %s, %s, %s, NOW(), %s::jsonb)
            ON CONFLICT (comment_id) DO UPDATE SET
                post_id = EXCLUDED.post_id,
                page_id = EXCLUDED.page_id,
                created_time = EXCLUDED.created_time,
                updated_at = NOW(),
                data = EXCLUDED.data
            """,
            (
                comment_id,
                str(item.get("post_id") or ""),
                str(item.get("page_id") or ""),
                _parse_graph_time_for_db(str(item.get("created_time") or "")),
                serialize_json(item),
            ),
        )


def _count_cached_facebook_posts() -> int:
    conn = _postgres_conn()
    if conn is None:
        return 0
    with conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM facebook_posts")
        row = cur.fetchone()
        return int(row[0] if row else 0)


def _list_cached_facebook_posts(limit: int, offset: int = 0) -> list[dict[str, Any]]:
    conn = _postgres_conn()
    if conn is None:
        return []
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT data::text
            FROM facebook_posts
            ORDER BY created_time DESC NULLS LAST, updated_at DESC
            LIMIT %s OFFSET %s
            """,
            (max(1, min(limit, 100)), max(0, offset)),
        )
        return [json.loads(row[0]) for row in cur.fetchall()]


def _get_cached_facebook_post(post_id: str) -> dict[str, Any] | None:
    post_id = str(post_id or "")
    if not post_id:
        return None
    conn = _postgres_conn()
    if conn is None:
        return None
    with conn, conn.cursor() as cur:
        cur.execute("SELECT data::text FROM facebook_posts WHERE post_id = %s", (post_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None


def _list_cached_facebook_posts_since(since: datetime, limit: int = 500) -> list[dict[str, Any]]:
    conn = _postgres_conn()
    if conn is None:
        return []
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT data::text
            FROM facebook_posts
            WHERE created_time >= %s
            ORDER BY created_time DESC NULLS LAST, updated_at DESC
            LIMIT %s
            """,
            (since, max(1, min(limit, 1000))),
        )
        return [json.loads(row[0]) for row in cur.fetchall()]


def _list_cached_facebook_comments(limit: int) -> list[dict[str, Any]]:
    conn = _postgres_conn()
    if conn is None:
        return []
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT data::text
            FROM facebook_comments
            ORDER BY created_time DESC NULLS LAST, updated_at DESC
            LIMIT %s
            """,
            (max(1, min(limit, 100)),),
        )
        return [json.loads(row[0]) for row in cur.fetchall()]


def _upsert_facebook_conversation(item: dict[str, Any]) -> None:
    conn = _postgres_conn()
    if conn is None:
        return
    conversation_id = str(item.get("conversation_id") or "")
    if not conversation_id:
        return
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facebook_conversations (conversation_id, page_id, updated_time, updated_at, data)
            VALUES (%s, %s, %s, NOW(), %s::jsonb)
            ON CONFLICT (conversation_id) DO UPDATE SET
                page_id = EXCLUDED.page_id,
                updated_time = EXCLUDED.updated_time,
                updated_at = NOW(),
                data = EXCLUDED.data
            """,
            (
                conversation_id,
                str(item.get("page_id") or ""),
                _parse_graph_time_for_db(str(item.get("updated_time") or "")),
                serialize_json(item),
            ),
        )


def _list_cached_facebook_conversations(limit: int) -> list[dict[str, Any]]:
    conn = _postgres_conn()
    if conn is None:
        return []
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT data::text
            FROM facebook_conversations
            ORDER BY updated_time DESC NULLS LAST, updated_at DESC
            LIMIT %s
            """,
            (max(1, min(limit, 100)),),
        )
        return [json.loads(row[0]) for row in cur.fetchall()]


def _get_cached_facebook_conversation(conversation_id: str) -> dict[str, Any] | None:
    conn = _postgres_conn()
    if conn is None:
        return None
    with conn, conn.cursor() as cur:
        cur.execute("SELECT data::text FROM facebook_conversations WHERE conversation_id = %s", (conversation_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None


def _upsert_facebook_message(item: dict[str, Any]) -> None:
    conn = _postgres_conn()
    if conn is None:
        return
    message_id = str(item.get("message_id") or "")
    if not message_id:
        return
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facebook_messages (message_id, conversation_id, page_id, customer_id, created_time, updated_at, data)
            VALUES (%s, %s, %s, %s, %s, NOW(), %s::jsonb)
            ON CONFLICT (message_id) DO UPDATE SET
                conversation_id = EXCLUDED.conversation_id,
                page_id = EXCLUDED.page_id,
                customer_id = EXCLUDED.customer_id,
                created_time = EXCLUDED.created_time,
                updated_at = NOW(),
                data = EXCLUDED.data
            """,
            (
                message_id,
                str(item.get("conversation_id") or ""),
                str(item.get("page_id") or ""),
                str(item.get("customer_id") or ""),
                _parse_graph_time_for_db(str(item.get("created_time") or "")),
                serialize_json(item),
            ),
        )


def _get_cached_facebook_message(message_id: str) -> dict[str, Any] | None:
    conn = _postgres_conn()
    if conn is None:
        return None
    with conn, conn.cursor() as cur:
        cur.execute("SELECT data::text FROM facebook_messages WHERE message_id = %s", (message_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None


def _list_cached_facebook_messages(conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
    conn = _postgres_conn()
    if conn is None or not conversation_id:
        return []
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT data::text
            FROM facebook_messages
            WHERE conversation_id = %s
            ORDER BY created_time ASC NULLS LAST, updated_at ASC
            LIMIT %s
            """,
            (conversation_id, max(1, min(limit, 200))),
        )
        return [json.loads(row[0]) for row in cur.fetchall()]


def _list_latest_cached_facebook_messages(conversation_id: str, limit: int = 1) -> list[dict[str, Any]]:
    conn = _postgres_conn()
    if conn is None or not conversation_id:
        return []
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT data::text
            FROM facebook_messages
            WHERE conversation_id = %s
            ORDER BY created_time DESC NULLS LAST, updated_at DESC
            LIMIT %s
            """,
            (conversation_id, max(1, min(limit, 200))),
        )
        rows = [json.loads(row[0]) for row in cur.fetchall()]
    return list(reversed(rows))


def _list_latest_cached_facebook_messages_by_conversation(
    conversation_ids: list[str], limit: int = 1
) -> dict[str, list[dict[str, Any]]]:
    ids = [str(item) for item in conversation_ids if item]
    if not ids:
        return {}
    conn = _postgres_conn()
    if conn is None:
        return {}
    limit = max(1, min(limit, 200))
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH ranked AS (
                SELECT
                    conversation_id,
                    data::text AS payload,
                    ROW_NUMBER() OVER (
                        PARTITION BY conversation_id
                        ORDER BY created_time DESC NULLS LAST, updated_at DESC
                    ) AS row_number
                FROM facebook_messages
                WHERE conversation_id = ANY(%s)
            )
            SELECT conversation_id, payload
            FROM ranked
            WHERE row_number <= %s
            ORDER BY conversation_id, row_number ASC
            """,
            (ids, limit),
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for conversation_id, payload in cur.fetchall():
            grouped[str(conversation_id)].append(json.loads(payload))
    return {conversation_id: list(reversed(messages)) for conversation_id, messages in grouped.items()}


def _upsert_facebook_stats(stat_key: str, payload: dict[str, Any]) -> None:
    conn = _postgres_conn()
    if conn is None:
        return
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facebook_stats (stat_key, updated_at, data)
            VALUES (%s, NOW(), %s::jsonb)
            ON CONFLICT (stat_key) DO UPDATE SET
                updated_at = NOW(),
                data = EXCLUDED.data
            """,
            (stat_key, serialize_json(payload)),
        )


def _get_cached_facebook_stats(stat_key: str) -> dict[str, Any] | None:
    conn = _postgres_conn()
    if conn is None:
        return None
    with conn, conn.cursor() as cur:
        cur.execute("SELECT data::text FROM facebook_stats WHERE stat_key = %s", (stat_key,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None


def _facebook_posts_payload(
    posts: list[dict[str, Any]],
    page_count: int,
    warnings: list[str] | None = None,
    *,
    total: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    selected = posts
    total_count = len(selected) if total is None else total
    return {
        "total": total_count,
        "limit": max(1, min(limit, 100)),
        "offset": max(0, offset),
        "has_more": max(0, offset) + len(selected) < total_count,
        "page_count": page_count,
        "totals": {
            "posted_7d": sum(1 for post in selected if post.get("posted_7d")),
            "scheduled": 0,
            "reach": sum(_safe_int(post.get("reach")) for post in selected),
            "views": sum(_safe_int(post.get("views") or post.get("impressions")) for post in selected),
            "engagement": sum(_safe_int(post.get("engagement")) for post in selected),
            "clicks": sum(_safe_int(post.get("clicks")) for post in selected),
            "comments": sum(_safe_int(post.get("comments")) for post in selected),
            "reactions": sum(_safe_int(post.get("reactions")) for post in selected),
            "shares": sum(_safe_int(post.get("shares")) for post in selected),
            "analytics_available": sum(1 for post in selected if post.get("analytics_status") in {"available", "partial", "stale"}),
        },
        "posts": selected,
        "warnings": (warnings or [])[:20],
    }


def _facebook_comments_payload(comments: list[dict[str, Any]], page_count: int, warnings: list[str] | None = None) -> dict[str, Any]:
    selected = comments
    return {
        "total": len(selected),
        "page_count": page_count,
        "totals": {
            "pending": sum(1 for item in selected if item.get("status") == "pending"),
            "negative": sum(1 for item in selected if item.get("sentiment") == "negative"),
            "question": sum(1 for item in selected if item.get("sentiment") == "question"),
            "positive": sum(1 for item in selected if item.get("sentiment") == "positive"),
            "neutral": sum(1 for item in selected if item.get("sentiment") == "neutral"),
        },
        "comments": selected,
        "warnings": (warnings or [])[:20],
    }


def _fetch_post_analytics(client: httpx.Client, base_url: str, post_id: str, token: str) -> tuple[dict[str, int], list[str]]:
    metrics, errors = FacebookGraphClient(client, base_url, token).fetch_post_analytics(post_id)
    warnings = [f"{post_id}: {target} unavailable - {message}" for target, message in errors.items()]
    return metrics, warnings


def _post_analytics_status(metrics: dict[str, int], errors: dict[str, str]) -> str:
    has_value = any(_safe_int(metrics.get(key)) for key in POST_ANALYTICS_KEYS)
    if has_value and errors:
        return "partial"
    if has_value:
        return "available"
    return "error" if errors else "empty"


def _preserve_cached_post_metrics(post_record: dict[str, Any]) -> dict[str, Any]:
    cached = _get_cached_facebook_post(str(post_record.get("post_id") or ""))
    if not cached:
        return post_record
    metric_keys = [*POST_ANALYTICS_KEYS, "views"]
    preserved = False
    for key in metric_keys:
        if _safe_int(post_record.get(key)) == 0 and _safe_int(cached.get(key)) > 0:
            post_record[key] = _safe_int(cached.get(key))
            preserved = True
    if preserved and post_record.get("analytics_status") in {"empty", "error"}:
        post_record["analytics_status"] = "stale"
    return post_record


def _facebook_post_record(
    *,
    page: dict[str, Any],
    post: dict[str, Any],
    analytics: dict[str, int],
    analytics_errors: dict[str, str],
    since_7d: datetime,
) -> dict[str, Any]:
    created_time = str(post.get("created_time") or "")
    created_dt = _parse_graph_time(created_time)
    analytics_error_types = {key: _graph_error_kind(value) for key, value in analytics_errors.items()}
    post_record = {
        "post_id": str(post.get("id") or ""),
        "page_id": str(page.get("page_id") or ""),
        "page_name": page.get("name") or "",
        "page_picture_url": page.get("picture_url") or "",
        "message": post.get("message") or "",
        "created_time": created_time,
        "type": post.get("type") or post.get("status_type") or "post",
        "status": "posted",
        "permalink_url": post.get("permalink_url") or "",
        "full_picture": post.get("full_picture") or "",
        "reach": analytics["reach"],
        "impressions": analytics["impressions"],
        "views": analytics["impressions"],
        "engagement": analytics["engagement"],
        "clicks": analytics["clicks"],
        "comments": analytics["comments"],
        "reactions": analytics["reactions"],
        "shares": analytics["shares"],
        "analytics_status": _post_analytics_status(analytics, analytics_errors),
        "analytics_errors": analytics_errors,
        "analytics_error_types": analytics_error_types,
        "analytics_synced_at": _now(),
        "posted_7d": bool(created_dt and created_dt >= since_7d),
    }
    return _preserve_cached_post_metrics(post_record)


def _post_analytics_from_graph_payload(post: dict[str, Any]) -> dict[str, int]:
    insights = post.get("insights") if isinstance(post.get("insights"), dict) else {}
    comments_summary = ((post.get("comments") or {}).get("summary") or {}) if isinstance(post.get("comments"), dict) else {}
    reactions_summary = ((post.get("reactions") or {}).get("summary") or {}) if isinstance(post.get("reactions"), dict) else {}
    shares_payload = post.get("shares") if isinstance(post.get("shares"), dict) else {}
    return {
        "reach": _post_insight_total(insights, "post_impressions_unique"),
        "impressions": _post_insight_total(insights, "post_impressions"),
        "engagement": _post_insight_total(insights, "post_engaged_users"),
        "clicks": _post_insight_total(insights, "post_clicks"),
        "comments": _safe_int(comments_summary.get("total_count")),
        "reactions": _safe_int(reactions_summary.get("total_count")),
        "shares": _safe_int(shares_payload.get("count")),
    }


def _merge_post_analytics(primary: dict[str, int], fallback: dict[str, int]) -> dict[str, int]:
    return {key: _safe_int(primary.get(key)) or _safe_int(fallback.get(key)) for key in ["reach", "impressions", "engagement", "clicks", "comments", "reactions", "shares"]}


def _empty_facebook_stats(days: int, page_count: int, warnings: list[str] | None = None) -> dict[str, Any]:
    normalized_days = max(1, min(days, 30))
    now = datetime.now(timezone.utc)
    labels = [(now - timedelta(days=normalized_days - index - 1)).date().isoformat() for index in range(normalized_days)]
    return {
        "days": normalized_days,
        "page_count": page_count,
        "totals": {"reach": 0, "engagement": 0, "likes": 0, "shares": 0, "comments": 0, "ctr": 0, "posts": 0},
        "series": [{"date": label, "reach": 0, "engagement": 0} for label in labels],
        "top_posts": [],
        "best_posting_time": "",
        "content_performance": [],
        "warnings": (warnings or [])[:20],
        "cached": False,
    }


def facebook_posts(limit: int = 50, offset: int = 0, max_pages: int = 25) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    cached = _list_cached_facebook_posts(limit, offset)
    total = _count_cached_facebook_posts()
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    warnings = [] if cached else ["No cached Facebook posts yet. Run sync to fetch posts from Graph API."]
    return _facebook_posts_payload(cached, len(pages), warnings, total=total, limit=limit, offset=offset)


def sync_facebook_posts(limit: int = 50, max_pages: int = 25) -> dict[str, Any]:
    settings = get_settings()
    limit = max(1, min(limit, 100))
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    now = datetime.now(timezone.utc)
    since_7d = now - timedelta(days=7)
    posts: list[dict[str, Any]] = []
    warnings: list[str] = []

    with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as client, ThreadPoolExecutor(max_workers=8) as executor:
        for page in pages:
            remaining = limit
            after = None
            graph = FacebookGraphClient(client, base_url, str(page["page_access_token"]))
            try:
                while remaining > 0:
                    posts_payload = graph.fetch_post_page(str(page["page_id"]), limit=min(25, remaining), after=after)
                    batch = posts_payload.get("data") or []
                    if not batch:
                        break
                    futures = {
                        executor.submit(graph.fetch_post_analytics, str(post.get("id") or "")): post
                        for post in batch
                        if str(post.get("id") or "")
                    }
                    for future in as_completed(futures):
                        post = futures[future]
                        post_id = str(post.get("id") or "")
                        try:
                            analytics, analytics_errors = future.result()
                        except httpx.HTTPError as error:
                            analytics = {key: 0 for key in POST_ANALYTICS_KEYS}
                            analytics_errors = {"post": _graph_error_message(error)}
                        except Exception as error:
                            analytics = {key: 0 for key in POST_ANALYTICS_KEYS}
                            analytics_errors = {"post": str(error)}
                        for target, message in analytics_errors.items():
                            warnings.append(f"{post_id}: {target} unavailable - {message}")
                        post_record = _facebook_post_record(
                            page=page,
                            post=post,
                            analytics=analytics,
                            analytics_errors=analytics_errors,
                            since_7d=since_7d,
                        )
                        posts.append(post_record)
                        _upsert_facebook_post(post_record)
                    remaining -= len(batch)
                    after = ((posts_payload.get("paging") or {}).get("cursors") or {}).get("after")
                    if not after:
                        break
            except httpx.HTTPError as error:
                warnings.append(f"{page.get('name') or page.get('page_id')}: posts unavailable - {_graph_error_message(error)}")
                continue

    posts.sort(key=lambda item: item.get("created_time") or "", reverse=True)
    total = _count_cached_facebook_posts()
    return _facebook_posts_payload(posts[:limit], len(pages), warnings, total=total or len(posts), limit=limit, offset=0)


def facebook_comments(limit: int = 50, max_pages: int = 25) -> dict[str, Any]:
    cached = _list_cached_facebook_comments(limit)
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    warnings = [] if cached else ["No cached Facebook comments yet. Run sync to fetch comments from Graph API."]
    return _facebook_comments_payload(cached, len(pages), warnings)


def sync_facebook_comments(limit: int = 50, max_pages: int = 25) -> dict[str, Any]:
    settings = get_settings()
    limit = max(1, min(limit, 100))
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    comments: list[dict[str, Any]] = []
    warnings: list[str] = []
    fields = "id,message,created_time,permalink_url,comments.limit(10){id,message,created_time,from,like_count,comment_count,parent}"

    with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
        for page in pages:
            try:
                response = client.get(
                    f"{base_url}/{page['page_id']}/posts",
                    params={
                        "fields": fields,
                        "limit": 10,
                        "access_token": page["page_access_token"],
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError as error:
                warnings.append(f"{page.get('name') or page.get('page_id')}: comments unavailable - {_graph_error_message(error)}")
                continue

            for post in response.json().get("data") or []:
                post_id = str(post.get("id") or "")
                post_message = str(post.get("message") or "")
                post_link = str(post.get("permalink_url") or "")
                nested_comments = ((post.get("comments") or {}).get("data") or [])
                if nested_comments:
                    for comment in nested_comments:
                        comments.append(
                            _normalize_facebook_comment(
                                comment,
                                page=page,
                                post_id=post_id,
                                post_message=post_message,
                                post_link=post_link,
                            )
                        )
                        _upsert_facebook_comment(comments[-1])
                    continue

                comment_payload: dict[str, Any] | None = None
                attempted_ids = [post_id]
                if "_" in post_id:
                    attempted_ids.append(post_id.split("_", 1)[1])
                for target_id in attempted_ids:
                    try:
                        comment_response = client.get(
                            f"{base_url}/{target_id}/comments",
                            params={
                                "fields": "id,message,created_time,from,like_count,comment_count,parent",
                                "limit": 10,
                                "access_token": page["page_access_token"],
                            },
                        )
                        comment_response.raise_for_status()
                        comment_payload = comment_response.json()
                        break
                    except httpx.HTTPError as error:
                        warnings.append(
                            f"{page.get('name') or page.get('page_id')}: direct comments unavailable for {target_id} - {_graph_error_message(error)}"
                        )
                if comment_payload is None:
                    continue
                for comment in comment_payload.get("data") or []:
                    comments.append(
                        _normalize_facebook_comment(
                            comment,
                            page=page,
                            post_id=post_id,
                            post_message=post_message,
                            post_link=post_link,
                        )
                    )
                    _upsert_facebook_comment(comments[-1])

    if not comments and pages and not warnings:
        warnings.append("Graph API did not return comments for recent posts. Check whether the page has comments and whether pages_read_user_content is available for this app.")

    comments.sort(key=lambda item: item.get("created_time") or "", reverse=True)
    return _facebook_comments_payload(comments[:limit], len(pages), warnings)


def _message_participant_name(participant: dict[str, Any]) -> str:
    return str(participant.get("name") or participant.get("email") or participant.get("id") or "Facebook User")


def _normalize_message_attachment(raw: dict[str, Any]) -> dict[str, Any]:
    if {"type", "url", "preview_url"}.issubset(raw.keys()):
        return {
            "attachment_id": str(raw.get("attachment_id") or raw.get("id") or ""),
            "type": str(raw.get("type") or "file"),
            "mime_type": str(raw.get("mime_type") or ""),
            "name": str(raw.get("name") or ""),
            "url": str(raw.get("url") or ""),
            "preview_url": str(raw.get("preview_url") or ""),
            "size": _safe_int(raw.get("size")),
        }
    image_data = raw.get("image_data") or {}
    video_data = raw.get("video_data") or {}
    payload = raw.get("payload") or {}
    target = raw.get("target") or {}
    subattachments = ((raw.get("subattachments") or {}).get("data")) or []
    first_subattachment = subattachments[0] if subattachments and isinstance(subattachments[0], dict) else {}
    first_sub_payload = first_subattachment.get("payload") or {}
    mime_type = str(raw.get("mime_type") or image_data.get("mime_type") or video_data.get("mime_type") or payload.get("mime_type") or "")
    image_url = str(
        image_data.get("url")
        or image_data.get("preview_url")
        or payload.get("url")
        or payload.get("src")
        or ((payload.get("media") or {}).get("image") or {}).get("src")
        or first_sub_payload.get("url")
        or first_sub_payload.get("src")
        or ""
    )
    video_url = str(video_data.get("url") or video_data.get("preview_url") or payload.get("url") or "")
    file_url = str(
        raw.get("file_url")
        or image_url
        or video_url
        or raw.get("url")
        or target.get("url")
        or first_subattachment.get("url")
        or ""
    )
    raw_type = str(raw.get("type") or "").lower()
    url_for_guess = file_url.lower()
    attachment_type = "file"
    if (
        raw_type == "image"
        or raw_type == "photo"
        or mime_type.startswith("image/")
        or image_url
        or any(url_for_guess.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"])
    ):
        attachment_type = "image"
    elif (
        raw_type == "video"
        or mime_type.startswith("video/")
        or video_url
        or any(url_for_guess.endswith(ext) for ext in [".mp4", ".mov", ".webm", ".m4v", ".avi"])
    ):
        attachment_type = "video"
    elif raw_type == "audio" or mime_type.startswith("audio/") or any(url_for_guess.endswith(ext) for ext in [".mp3", ".wav", ".m4a", ".ogg"]):
        attachment_type = "audio"
    return {
        "attachment_id": str(raw.get("id") or ""),
        "type": attachment_type,
        "mime_type": mime_type,
        "name": str(raw.get("name") or raw.get("title") or ""),
        "url": file_url,
        "preview_url": str(image_data.get("preview_url") or video_data.get("preview_url") or image_url or ""),
        "size": _safe_int(raw.get("file_size") or raw.get("size")),
    }


def _attachment_has_media_url(attachment: dict[str, Any]) -> bool:
    return bool(str(attachment.get("url") or attachment.get("preview_url") or "").strip())


def _message_fallback_label(message_text: str, attachments: list[dict[str, Any]], shares: dict[str, Any], sticker: dict[str, Any]) -> str:
    if message_text:
        return ""
    if attachments:
        return "Đã gửi tệp đính kèm"
    if shares:
        return "Đã chia sẻ liên kết"
    if sticker:
        return "Đã gửi sticker"
    return "Tin nhắn đặc biệt"


def _normalize_reply_to(raw: dict[str, Any] | None) -> dict[str, Any]:
    payload = raw or {}
    attachments = [
        _normalize_message_attachment(item)
        for item in (((payload.get("attachments") or {}).get("data")) or [])
        if isinstance(item, dict)
    ]
    message_text = str(payload.get("message") or "")
    return {
        "mid": str(payload.get("id") or payload.get("mid") or ""),
        "message": message_text,
        "created_time": str(payload.get("created_time") or ""),
        "from_id": str((payload.get("from") or {}).get("id") or ""),
        "from_name": _message_participant_name(payload.get("from") or {}),
        "attachments": attachments,
        "fallback_label": _message_fallback_label(
            message_text,
            attachments,
            payload.get("shares") or {},
            payload.get("sticker") or {},
        ),
    }


def _reply_has_displayable_content(reply: dict[str, Any]) -> bool:
    if not reply:
        return False
    if reply.get("message"):
        return True
    return any(_attachment_has_media_url(item) for item in (reply.get("attachments") or []))


def _merge_message_attachments(primary: list[dict[str, Any]], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in fallback + primary:
        attachment_id = str(item.get("attachment_id") or "")
        key = attachment_id or f"{item.get('type')}::{item.get('name')}::{item.get('url')}::{item.get('preview_url')}"
        existing = merged.get(key) or {}
        merged[key] = {
            **item,
            "attachment_id": attachment_id or existing.get("attachment_id") or "",
            "type": item.get("type") or existing.get("type") or "file",
            "mime_type": item.get("mime_type") or existing.get("mime_type") or "",
            "name": item.get("name") or existing.get("name") or "",
            "url": item.get("url") or existing.get("url") or "",
            "preview_url": item.get("preview_url") or existing.get("preview_url") or "",
            "size": item.get("size") or existing.get("size") or 0,
        }
    return list(merged.values())


def _fetch_message_reference(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    message_id: str,
) -> dict[str, Any]:
    if not message_id:
        return {}
    try:
        response = client.get(
            f"{base_url}/{message_id}",
            params={"fields": "id,created_time,from,message,attachments,shares,sticker", "access_token": access_token},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return {}
    return _normalize_reply_to(response.json())


def _fetch_message_extras(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    message_id: str,
) -> dict[str, Any]:
    extras: dict[str, Any] = {"attachments": [], "shares": {}, "sticker": {}, "reply_to": {}}
    if not message_id:
        return extras
    try:
        response = client.get(
            f"{base_url}/{message_id}",
            params={"fields": MESSAGE_DETAIL_FIELDS, "access_token": access_token},
        )
        response.raise_for_status()
        payload = response.json()
        extras["shares"] = payload.get("shares") or {}
        extras["sticker"] = payload.get("sticker") or {}
        extras["reply_to"] = _normalize_reply_to(payload.get("reply_to") or {})
        reply_mid = str((extras["reply_to"] or {}).get("mid") or "")
        if reply_mid and not _reply_has_displayable_content(extras["reply_to"]):
            extras["reply_to"] = _fetch_message_reference(client, base_url, access_token, reply_mid) or extras["reply_to"]
        extras["attachments"] = [
            _normalize_message_attachment(item)
            for item in (((payload.get("attachments") or {}).get("data")) or [])
            if isinstance(item, dict)
        ]
    except httpx.HTTPError:
        return extras
    if extras["attachments"] and all(_attachment_has_media_url(item) for item in extras["attachments"]):
        return extras
    try:
        response = client.get(
            f"{base_url}/{message_id}/attachments",
            params={"access_token": access_token},
        )
        response.raise_for_status()
        payload = response.json()
        attachment_items = [
            _normalize_message_attachment(item)
            for item in (payload.get("data") or [])
            if isinstance(item, dict)
        ]
        extras["attachments"] = _merge_message_attachments(attachment_items, extras["attachments"])
    except httpx.HTTPError:
        return extras
    return extras


def _normalize_conversation(page: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    participants = (((raw.get("participants") or {}).get("data")) or [])
    page_id = str(page.get("page_id") or "")
    customer = next((item for item in participants if str(item.get("id") or "") != page_id), participants[0] if participants else {})
    messages = []
    for message in (((raw.get("messages") or {}).get("data")) or []):
        sender = message.get("from") or {}
        recipient = ((message.get("to") or {}).get("data") or [{}])[0]
        sender_id = str(sender.get("id") or "")
        attachments = [
            _normalize_message_attachment(item)
            for item in (((message.get("attachments") or {}).get("data")) or [])
            if isinstance(item, dict)
        ]
        message_text = str(message.get("message") or "")
        fallback_label = _message_fallback_label(message_text, attachments, message.get("shares") or {}, message.get("sticker") or {})
        reply_to = _normalize_reply_to(message.get("reply_to") or {})
        messages.append(
            {
                "message_id": str(message.get("id") or ""),
                "message": message_text,
                "created_time": str(message.get("created_time") or ""),
                "from_id": sender_id,
                "from_name": _message_participant_name(sender),
                "to_id": str(recipient.get("id") or ""),
                "to_name": _message_participant_name(recipient),
                "direction": "outbound" if sender_id == page_id else "inbound",
                "attachments": attachments,
                "fallback_label": fallback_label,
                "reply_to": reply_to,
            }
        )
    messages.sort(key=lambda item: item.get("created_time") or "")
    by_mid = {str(item.get("message_id") or ""): item for item in messages if item.get("message_id")}
    for item in messages:
        reply_to = item.get("reply_to") or {}
        reply_mid = str(reply_to.get("mid") or "")
        if not reply_mid or _reply_has_displayable_content(reply_to):
            continue
        source = by_mid.get(reply_mid)
        if not source:
            continue
        item["reply_to"] = {
            "mid": reply_mid,
            "message": str(source.get("message") or ""),
            "created_time": str(source.get("created_time") or ""),
            "from_id": str(source.get("from_id") or ""),
            "from_name": str(source.get("from_name") or ""),
            "attachments": source.get("attachments") or [],
            "fallback_label": str(source.get("fallback_label") or ""),
            "direction": str(source.get("direction") or ""),
        }
    last_message = messages[-1] if messages else {}
    return {
        "conversation_id": str(raw.get("id") or ""),
        "page_id": page_id,
        "page_name": page.get("name") or "",
        "page_picture_url": page.get("picture_url") or "",
        "customer_id": str(customer.get("id") or ""),
        "customer_name": _message_participant_name(customer),
        "snippet": raw.get("snippet") or last_message.get("message") or last_message.get("fallback_label") or "",
        "updated_time": str(raw.get("updated_time") or last_message.get("created_time") or ""),
        "unread_count": _safe_int(raw.get("unread_count")),
        "message_count": _safe_int(raw.get("message_count")) or len(messages),
        "messages": messages,
        "status": "unread" if _safe_int(raw.get("unread_count")) else "open",
    }


def _find_cached_conversation_by_participants(page_id: str, customer_id: str) -> dict[str, Any] | None:
    conn = _postgres_conn()
    if conn is None or not page_id or not customer_id:
        return None
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT data::text
            FROM facebook_conversations
            WHERE page_id = %s
            ORDER BY updated_time DESC NULLS LAST, updated_at DESC
            """,
            (page_id,),
        )
        for row in cur.fetchall():
            payload = json.loads(row[0])
            if str(payload.get("customer_id") or "") == customer_id:
                return payload
    return None


def _merge_conversation_messages(graph_messages: list[dict[str, Any]], stored_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in graph_messages + stored_messages:
        message_id = str(item.get("message_id") or "")
        key = message_id or f"{item.get('created_time')}::{item.get('from_id')}::{item.get('message')}"
        existing = merged.get(key) or {}
        merged[key] = {
            **existing,
            **item,
            "attachments": _merge_message_attachments(item.get("attachments") or [], existing.get("attachments") or []),
            "reply_to": item.get("reply_to") or existing.get("reply_to") or {},
            "fallback_label": item.get("fallback_label") or existing.get("fallback_label") or "",
        }
    ordered = sorted(merged.values(), key=lambda item: item.get("created_time") or "")
    by_mid = {str(item.get("message_id") or ""): item for item in ordered if item.get("message_id")}
    for item in ordered:
        reply_to = item.get("reply_to") or {}
        reply_mid = str(reply_to.get("mid") or "")
        if not reply_mid:
            continue
        if _reply_has_displayable_content(reply_to):
            continue
        source = by_mid.get(reply_mid) or _get_cached_facebook_message(reply_mid) or {}
        if not source:
            continue
        item["reply_to"] = {
            "mid": reply_mid,
            "message": str(source.get("message") or ""),
            "created_time": str(source.get("created_time") or ""),
            "from_id": str(source.get("from_id") or ""),
            "from_name": str(source.get("from_name") or ""),
            "attachments": source.get("attachments") or [],
            "fallback_label": str(source.get("fallback_label") or ""),
            "direction": str(source.get("direction") or ""),
        }
    return ordered


def _refresh_conversation_cache(
    conversation_id: str,
    page_id: str,
    customer_id: str,
    customer_name: str,
    page_name: str,
    page_picture_url: str = "",
) -> dict[str, Any]:
    existing = _get_cached_facebook_conversation(conversation_id) or {}
    messages = _list_cached_facebook_messages(conversation_id, 100)
    last_message = messages[-1] if messages else {}
    payload = {
        "conversation_id": conversation_id,
        "page_id": page_id,
        "page_name": page_name or existing.get("page_name") or "",
        "page_picture_url": page_picture_url or existing.get("page_picture_url") or "",
        "customer_id": customer_id,
        "customer_name": customer_name or existing.get("customer_name") or "Facebook User",
        "snippet": last_message.get("message") or last_message.get("fallback_label") or existing.get("snippet") or "",
        "updated_time": last_message.get("created_time") or existing.get("updated_time") or _now(),
        "unread_count": existing.get("unread_count") or 0,
        "message_count": len(messages),
        "messages": messages,
        "status": existing.get("status") or "open",
    }
    _upsert_facebook_conversation(payload)
    return payload


def mark_facebook_conversation_read(conversation_id: str) -> dict[str, Any]:
    conversation_id = (conversation_id or "").strip()
    if not conversation_id:
        raise RuntimeError("conversation_id is required.")
    conversation = _get_cached_facebook_conversation(conversation_id)
    if not conversation:
        raise RuntimeError("Facebook conversation not found.")
    page_id = str(conversation.get("page_id") or "")
    customer_id = str(conversation.get("customer_id") or "")
    page = next((item for item in _list_facebook_page_records() if str(item.get("page_id") or "") == page_id), None)
    if page and page.get("page_access_token") and customer_id:
        settings = get_settings()
        base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            try:
                response = client.post(
                    f"{base_url}/{page_id}/messages",
                    params={"access_token": page["page_access_token"]},
                    json={"recipient": {"id": customer_id}, "sender_action": "mark_seen"},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                pass
    conversation["unread_count"] = 0
    conversation["status"] = "open"
    conversation["read_at"] = _now()
    _upsert_facebook_conversation(conversation)
    _publish_facebook_conversation_synced(conversation)
    return conversation


def _publish_facebook_message_event(conversation: dict[str, Any], message: dict[str, Any], event_type: str = "facebook.message.upserted") -> None:
    publish_realtime_event(
        "facebook:messages",
        {
            "type": event_type,
            "conversation_id": conversation.get("conversation_id") or message.get("conversation_id") or "",
            "conversation": conversation,
            "message": message,
        },
    )


def _publish_facebook_conversation_synced(conversation: dict[str, Any], sync_job_id: str = "") -> None:
    latest_message = (conversation.get("messages") or [])[-1] if conversation.get("messages") else {}
    publish_realtime_event(
        "facebook:messages",
        {
            "type": "facebook.conversation.synced",
            "conversation_id": conversation.get("conversation_id") or "",
            "conversation": conversation,
            "message": latest_message,
            "sync_job_id": sync_job_id,
        },
    )


def _conversation_with_cached_messages(conversation: dict[str, Any], message_limit: int) -> dict[str, Any]:
    conversation_id = str(conversation.get("conversation_id") or "")
    message_limit = max(0, min(message_limit, 200))
    if message_limit <= 0:
        merged_messages: list[dict[str, Any]] = []
    elif message_limit == 1:
        graph_messages = (conversation.get("messages") or [])[-1:]
        stored_messages = _list_latest_cached_facebook_messages(conversation_id, 1)
        merged_messages = _merge_conversation_messages(graph_messages, stored_messages)[-1:]
    else:
        graph_messages = (conversation.get("messages") or [])[-message_limit:]
        stored_messages = _list_latest_cached_facebook_messages(conversation_id, message_limit)
        merged_messages = _merge_conversation_messages(graph_messages, stored_messages)[-message_limit:]
    conversation["messages"] = merged_messages
    conversation["message_count"] = max(int(conversation.get("message_count") or 0), len(merged_messages))
    if merged_messages:
        last_message = merged_messages[-1]
        conversation["snippet"] = last_message.get("message") or last_message.get("fallback_label") or conversation.get("snippet") or ""
        conversation["updated_time"] = last_message.get("created_time") or conversation.get("updated_time") or ""
    return conversation


def facebook_conversations(limit: int = 50, max_pages: int = 500, message_limit: int = 1) -> dict[str, Any]:
    conversations = _list_cached_facebook_conversations(limit)
    enriched: list[dict[str, Any]] = []
    if max(0, min(message_limit, 200)) == 1:
        latest_by_conversation = _list_latest_cached_facebook_messages_by_conversation(
            [str(item.get("conversation_id") or "") for item in conversations],
            1,
        )
        for conversation in conversations:
            conversation_id = str(conversation.get("conversation_id") or "")
            graph_latest = (conversation.get("messages") or [])[-1:]
            latest_messages = latest_by_conversation.get(conversation_id) or graph_latest
            conversation["messages"] = latest_messages[-1:]
            conversation["message_count"] = max(int(conversation.get("message_count") or 0), len(latest_messages))
            if latest_messages:
                last_message = latest_messages[-1]
                conversation["snippet"] = last_message.get("message") or last_message.get("fallback_label") or conversation.get("snippet") or ""
                conversation["updated_time"] = last_message.get("created_time") or conversation.get("updated_time") or ""
            enriched.append(conversation)
    else:
        for conversation in conversations:
            enriched.append(_conversation_with_cached_messages(conversation, message_limit))
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 1000))]
    warnings = [] if enriched else ["No cached Facebook conversations yet. Run sync to fetch inbox from Graph API."]
    return {
        "total": len(enriched),
        "page_count": len(pages),
        "conversations": enriched,
        "warnings": warnings[:20],
    }


def facebook_conversation_detail(conversation_id: str, message_limit: int = 100) -> dict[str, Any]:
    conversation_id = (conversation_id or "").strip()
    if not conversation_id:
        raise RuntimeError("conversation_id is required.")
    conversation = _get_cached_facebook_conversation(conversation_id)
    if not conversation:
        raise RuntimeError("Facebook conversation not found.")
    return _conversation_with_cached_messages(conversation, max(1, min(message_limit, 200)))


def debug_facebook_messages(conversation_id: str = "", message_id: str = "") -> dict[str, Any]:
    message_id = (message_id or "").strip()
    conversation_id = (conversation_id or "").strip()
    if message_id:
        message = _get_cached_facebook_message(message_id)
        return {"message_id": message_id, "message": message or {}, "found": bool(message)}
    if not conversation_id:
        raise RuntimeError("conversation_id or message_id is required.")
    conversation = _get_cached_facebook_conversation(conversation_id) or {}
    stored_messages = _list_cached_facebook_messages(conversation_id, 200)
    graph_messages = conversation.get("messages") or []
    merged_messages = _merge_conversation_messages(graph_messages, stored_messages)
    return {
        "conversation_id": conversation_id,
        "conversation_found": bool(conversation),
        "stored_count": len(stored_messages),
        "graph_count": len(graph_messages),
        "messages": [
            {
                "message_id": item.get("message_id"),
                "message": item.get("message"),
                "direction": item.get("direction"),
                "attachments": item.get("attachments") or [],
                "fallback_label": item.get("fallback_label"),
                "reply_to": item.get("reply_to") or {},
                "raw": item.get("raw") or {},
            }
            for item in merged_messages
        ],
    }


def sync_facebook_conversations(limit: int = 50, max_pages: int = 500, sync_job_id: str = "") -> dict[str, Any]:
    settings = get_settings()
    limit = max(1, min(limit, 100))
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 1000))]
    base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    conversations: list[dict[str, Any]] = []
    warnings: list[str] = []
    fields = "id,snippet,updated_time,unread_count,message_count,participants,messages.limit(30){id,message,created_time,from,to,shares,sticker,reply_to{id},attachments}"
    per_page_limit = max(1, min(limit, 25))

    with httpx.Client(timeout=httpx.Timeout(12.0, connect=3.0)) as client:
        for page in pages:
            after = None
            fetched_for_page = 0
            try:
                while fetched_for_page < per_page_limit:
                    params = {
                        "fields": fields,
                        "limit": min(25, per_page_limit - fetched_for_page),
                        "access_token": page["page_access_token"],
                    }
                    if after:
                        params["after"] = after
                    response = client.get(f"{base_url}/{page['page_id']}/conversations", params=params)
                    response.raise_for_status()
                    payload = response.json()
                    batch = payload.get("data") or []
                    if not batch:
                        break
                    for item in batch:
                        message_items = (((item.get("messages") or {}).get("data")) or [])
                        for message in message_items:
                            if not isinstance(message, dict):
                                continue
                            message_id = str(message.get("id") or "")
                            thread_attachments = [
                                _normalize_message_attachment(attachment)
                                for attachment in (((message.get("attachments") or {}).get("data")) or [])
                                if isinstance(attachment, dict)
                            ]
                            should_enrich = (
                                not message.get("message")
                                or not thread_attachments
                                or any(not _attachment_has_media_url(attachment) for attachment in thread_attachments)
                            )
                            if not should_enrich:
                                continue
                            extras = _fetch_message_extras(client, base_url, page["page_access_token"], message_id)
                            if extras.get("attachments"):
                                message["attachments"] = {"data": extras["attachments"]}
                            if extras.get("shares"):
                                message["shares"] = extras["shares"]
                            if extras.get("sticker"):
                                message["sticker"] = extras["sticker"]
                            if extras.get("reply_to"):
                                message["reply_to"] = extras["reply_to"]
                        conversation = _normalize_conversation(page, item)
                        conversations.append(conversation)
                        fetched_for_page += 1
                        _upsert_facebook_conversation(conversation)
                        for normalized_message in conversation.get("messages") or []:
                            if not isinstance(normalized_message, dict):
                                continue
                            _upsert_facebook_message(
                                {
                                    **normalized_message,
                                    "conversation_id": conversation.get("conversation_id") or "",
                                    "page_id": conversation.get("page_id") or "",
                                    "customer_id": conversation.get("customer_id") or "",
                                }
                            )
                        _publish_facebook_conversation_synced(conversation, sync_job_id)
                    after = ((payload.get("paging") or {}).get("cursors") or {}).get("after")
                    if not after or fetched_for_page >= per_page_limit:
                        break
            except httpx.HTTPError as error:
                warnings.append(f"{page.get('name') or page.get('page_id')}: conversations unavailable - {_graph_error_message(error)}")
                continue

    conversations.sort(key=lambda item: item.get("updated_time") or "", reverse=True)
    return {
        "total": len(conversations),
        "page_count": len(pages),
        "conversations": conversations[:limit],
        "warnings": warnings[:20],
    }


def run_facebook_conversation_sync_job(job_id: str, limit: int = 50) -> dict[str, Any]:
    job = get_facebook_sync_job(job_id) or {
        "job_id": job_id,
        "kind": "facebook_conversations_sync",
        "limit": limit,
        "status": "queued",
        "created_at": _now(),
    }
    job["status"] = "running"
    job["started_at"] = _now()
    _upsert_facebook_sync_job(job)
    try:
        result = sync_facebook_conversations(limit, sync_job_id=job_id)
        job["status"] = "completed"
        job["completed_at"] = _now()
        job["result"] = {
            "total": result.get("total", 0),
            "page_count": result.get("page_count", 0),
            "warnings": result.get("warnings", []),
        }
        _upsert_facebook_sync_job(job)
        publish_realtime_event(
            "facebook:messages",
            {
                "type": "facebook.conversations.sync.completed",
                "sync_job": job,
                "conversations": result.get("conversations", [])[:50],
            },
        )
        return job
    except Exception as exc:
        job["status"] = "failed"
        job["failed_at"] = _now()
        job["error"] = str(exc)
        _upsert_facebook_sync_job(job)
        publish_realtime_event("facebook:messages", {"type": "facebook.conversations.sync.failed", "sync_job": job})
        raise


def enqueue_facebook_conversation_sync(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    job = {
        "job_id": secrets.token_hex(16),
        "kind": "facebook_conversations_sync",
        "limit": limit,
        "status": "queued",
        "created_at": _now(),
        "queue": "inline",
    }
    _upsert_facebook_sync_job(job)
    settings = get_settings()
    queue = _rq_content_queue() if settings.queue_mode == "rq" else None
    if queue is not None:
        queue.enqueue(
            "app.facebook_pages.run_facebook_conversation_sync_job",
            kwargs={"job_id": job["job_id"], "limit": limit},
            job_timeout=900,
            result_ttl=86400,
            failure_ttl=604800,
        )
        job["queue"] = queue.name
        _upsert_facebook_sync_job(job)
        return job
    return run_facebook_conversation_sync_job(job["job_id"], limit)


def send_facebook_message(
    conversation_id: str,
    message: str = "",
    attachment_url: str = "",
    attachment_type: str = "image",
    attachment_name: str = "",
) -> dict[str, Any]:
    conversation_id = (conversation_id or "").strip()
    message = (message or "").strip()
    attachment_url = (attachment_url or "").strip()
    attachment_type = (attachment_type or "image").strip().lower()
    attachment_name = (attachment_name or "").strip()
    if not conversation_id:
        raise RuntimeError("conversation_id is required.")
    if not message and not attachment_url:
        raise RuntimeError("Message or attachment is required.")
    if attachment_type not in {"image", "video", "audio", "file"}:
        attachment_type = "file"
    conversation = _get_cached_facebook_conversation(conversation_id)
    if not conversation:
        raise RuntimeError("Conversation not found in local cache. Sync inbox first.")
    customer_id = str(conversation.get("customer_id") or "")
    page_id = str(conversation.get("page_id") or "")
    page = next((item for item in _list_facebook_page_records() if str(item.get("page_id") or "") == page_id), None)
    if not page or not page.get("page_access_token"):
        raise RuntimeError("Page token not found for this conversation.")
    if not customer_id:
        raise RuntimeError("Customer id is missing for this conversation.")

    settings = get_settings()
    base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    sent_ids: list[str] = []
    with httpx.Client(timeout=httpx.Timeout(12.0, connect=3.0)) as client:
        if attachment_url:
            response = client.post(
                f"{base_url}/{page_id}/messages",
                params={"access_token": page["page_access_token"]},
                json={
                    "recipient": {"id": customer_id},
                    "messaging_type": "RESPONSE",
                    "message": {
                        "attachment": {
                            "type": attachment_type,
                            "payload": {"url": attachment_url, "is_reusable": True},
                        }
                    },
                },
            )
            response.raise_for_status()
            payload = response.json()
            sent_ids.append(str(payload.get("message_id") or ""))
        if message:
            response = client.post(
                f"{base_url}/{page_id}/messages",
                params={"access_token": page["page_access_token"]},
                json={
                    "recipient": {"id": customer_id},
                    "messaging_type": "RESPONSE",
                    "message": {"text": message},
                },
            )
            response.raise_for_status()
            payload = response.json()
            sent_ids.append(str(payload.get("message_id") or ""))

    sent_message = {
        "message_id": sent_ids[-1] if sent_ids else "",
        "conversation_id": conversation_id,
        "page_id": page_id,
        "customer_id": customer_id,
        "message": message,
        "created_time": _now(),
        "from_id": page_id,
        "from_name": conversation.get("page_name") or "Page",
        "to_id": customer_id,
        "to_name": conversation.get("customer_name") or "Facebook User",
        "direction": "outbound",
        "attachments": [
            {
                "attachment_id": "",
                "type": attachment_type,
                "mime_type": "",
                "name": attachment_name,
                "url": attachment_url,
                "preview_url": attachment_url if attachment_type == "image" else "",
                "size": 0,
            }
        ] if attachment_url else [],
        "fallback_label": "Đã gửi tệp đính kèm" if attachment_url and not message else "",
        "reply_to": {},
    }
    _upsert_facebook_message(sent_message)
    conversation = _refresh_conversation_cache(
        conversation_id,
        page_id,
        customer_id,
        str(conversation.get("customer_name") or ""),
        str(conversation.get("page_name") or ""),
        str(conversation.get("page_picture_url") or ""),
    )
    conversation["status"] = "open"
    conversation["unread_count"] = 0
    _upsert_facebook_conversation(conversation)
    _publish_facebook_message_event(conversation, sent_message, "facebook.message.sent")
    return {"sent": True, "conversation_id": conversation_id, "message_id": sent_message["message_id"]}


def verify_facebook_webhook_signature(body: bytes, signature: str | None) -> bool:
    settings = get_settings()
    if not settings.facebook_app_secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.facebook_app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    provided = signature.split("=", 1)[1].strip()
    return hmac.compare_digest(expected, provided)


def process_facebook_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    entries = payload.get("entry") or []
    processed = 0
    for entry in entries:
        for messaging in entry.get("messaging") or []:
            message = messaging.get("message") or {}
            message_id = str(message.get("mid") or "")
            if not message_id:
                continue
            sender = messaging.get("sender") or {}
            recipient = messaging.get("recipient") or {}
            is_echo = bool(message.get("is_echo"))
            page_id = str(sender.get("id") or "") if is_echo else str(recipient.get("id") or "")
            customer_id = str(recipient.get("id") or "") if is_echo else str(sender.get("id") or "")
            if not page_id or not customer_id:
                continue
            page = next((item for item in _list_facebook_page_records() if str(item.get("page_id") or "") == page_id), None) or {}
            existing_conversation = _find_cached_conversation_by_participants(page_id, customer_id) or {}
            conversation_id = str(existing_conversation.get("conversation_id") or f"psid:{page_id}:{customer_id}")
            attachments = [
                _normalize_message_attachment(item)
                for item in (message.get("attachments") or [])
                if isinstance(item, dict)
            ]
            message_text = str(message.get("text") or "")
            shares = message.get("shares") or {}
            sticker = message.get("sticker") or {}
            reply_to_mid = str(((message.get("reply_to") or {}).get("mid")) or "")
            quoted = _get_cached_facebook_message(reply_to_mid) if reply_to_mid else None
            normalized = {
                "message_id": message_id,
                "conversation_id": conversation_id,
                "page_id": page_id,
                "customer_id": customer_id,
                "message": message_text,
                "created_time": datetime.fromtimestamp(
                    int(messaging.get("timestamp") or 0) / 1000,
                    tz=timezone.utc,
                ).isoformat() if messaging.get("timestamp") else _now(),
                "from_id": str(sender.get("id") or ""),
                "from_name": existing_conversation.get("customer_name") if not is_echo else page.get("name") or "Page",
                "to_id": str(recipient.get("id") or ""),
                "to_name": page.get("name") or "Facebook Page" if not is_echo else existing_conversation.get("customer_name") or "Facebook User",
                "direction": "outbound" if is_echo else "inbound",
                "attachments": attachments,
                "fallback_label": _message_fallback_label(message_text, attachments, shares, sticker),
                "reply_to": {
                    "mid": reply_to_mid,
                    "message": str((quoted or {}).get("message") or ""),
                    "fallback_label": str((quoted or {}).get("fallback_label") or ""),
                    "attachments": (quoted or {}).get("attachments") or [],
                    "direction": str((quoted or {}).get("direction") or ""),
                } if reply_to_mid else {},
                "raw": message,
            }
            _upsert_facebook_message(normalized)
            conversation = _refresh_conversation_cache(
                conversation_id,
                page_id,
                customer_id,
                str(existing_conversation.get("customer_name") or ""),
                str(page.get("name") or existing_conversation.get("page_name") or ""),
                str(page.get("picture_url") or existing_conversation.get("page_picture_url") or ""),
            )
            if is_echo:
                conversation["unread_count"] = 0
                conversation["status"] = "open"
            else:
                conversation["unread_count"] = _safe_int(existing_conversation.get("unread_count")) + 1
                conversation["status"] = "unread"
            _upsert_facebook_conversation(conversation)
            _publish_facebook_message_event(conversation, normalized)
            processed += 1
    return {"processed": processed}


def facebook_aggregate_stats(days: int = 7, max_pages: int = 25) -> dict[str, Any]:
    cache_key = f"{max(1, min(days, 30))}:{max(1, min(max_pages, 100))}"
    cached = _get_cached_facebook_stats(cache_key)
    if cached:
        cached["cached"] = True
        return cached
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    payload = _facebook_stats_from_cached_posts(days, len(pages), ["Using cached post analytics. Sync Facebook posts to refresh source metrics."])
    if _safe_int((payload.get("totals") or {}).get("posts")):
        payload["cached"] = True
        _upsert_facebook_stats(cache_key, payload)
        return payload
    return _empty_facebook_stats(days, len(pages), ["No cached Facebook stats yet. Sync Facebook posts first to populate local analytics."])


def _facebook_stats_from_cached_posts(days: int, page_count: int, warnings: list[str] | None = None) -> dict[str, Any]:
    normalized_days = max(1, min(days, 30))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=normalized_days)
    posts = _list_cached_facebook_posts_since(since, 1000)
    reach_by_day: defaultdict[str, int] = defaultdict(int)
    engagement_by_day: defaultdict[str, int] = defaultdict(int)
    comments = 0
    shares = 0
    reactions = 0
    clicks = 0
    analytics_counts: defaultdict[str, int] = defaultdict(int)
    analytics_error_types: defaultdict[str, int] = defaultdict(int)
    content_types: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"posts": 0, "reach": 0})
    hour_buckets: defaultdict[int, int] = defaultdict(int)
    top_posts: list[dict[str, Any]] = []

    for post in posts:
        created_time = str(post.get("created_time") or "")
        created_dt = _parse_graph_time(created_time)
        label = _date_key(created_time)
        reach = _safe_int(post.get("reach"))
        engagement = _safe_int(post.get("engagement"))
        post_comments = _safe_int(post.get("comments"))
        post_shares = _safe_int(post.get("shares"))
        post_reactions = _safe_int(post.get("reactions"))
        post_clicks = _safe_int(post.get("clicks"))
        analytics_status = str(post.get("analytics_status") or "empty")
        analytics_counts[analytics_status] += 1
        for kind in (post.get("analytics_error_types") or {}).values():
            analytics_error_types[str(kind or "api_error")] += 1
        if label:
            reach_by_day[label] += reach
            engagement_by_day[label] += engagement
        comments += post_comments
        shares += post_shares
        reactions += post_reactions
        clicks += post_clicks
        content_type = str(post.get("type") or post.get("status") or "unknown").replace("_", " ").title()
        content_types[content_type]["posts"] += 1
        content_types[content_type]["reach"] += reach
        if created_dt:
            hour_buckets[created_dt.hour] += engagement
        top_posts.append(
            {
                "page_name": post.get("page_name") or "",
                "message": (post.get("message") or "Untitled post")[:120],
                "reach": reach,
                "engagement": engagement,
                "comments": post_comments,
                "reactions": post_reactions,
                "shares": post_shares,
                "analytics_status": analytics_status,
            }
        )

    reach_total = sum(reach_by_day.values())
    engagement_total = sum(engagement_by_day.values())
    ctr = round((engagement_total / reach_total) * 100, 2) if reach_total else 0.0
    covered_posts = sum(analytics_counts.get(status, 0) for status in ["available", "partial", "stale"])
    analytics_coverage = round((covered_posts / len(posts)) * 100, 2) if posts else 0.0
    labels = [(since + timedelta(days=index + 1)).date().isoformat() for index in range(normalized_days)]
    best_hour = max(hour_buckets.items(), key=lambda item: item[1])[0] if hour_buckets else None
    content_performance = []
    for label, data in content_types.items():
        count = data["posts"]
        content_performance.append({"type": label, "posts": count, "avg_reach": round(data["reach"] / count) if count else 0})
    content_performance.sort(key=lambda item: item["avg_reach"], reverse=True)
    top_posts.sort(key=lambda item: item["reach"] + item["engagement"], reverse=True)
    return {
        "days": normalized_days,
        "page_count": page_count,
        "totals": {
            "reach": reach_total,
            "engagement": engagement_total,
            "likes": 0,
            "reactions": reactions,
            "shares": shares,
            "comments": comments,
            "clicks": clicks,
            "ctr": ctr,
            "posts": len(posts),
            "analytics_coverage": analytics_coverage,
            "analytics_available": analytics_counts.get("available", 0),
            "analytics_partial": analytics_counts.get("partial", 0),
            "analytics_stale": analytics_counts.get("stale", 0),
            "analytics_error": analytics_counts.get("error", 0),
            "analytics_empty": analytics_counts.get("empty", 0),
        },
        "analytics_breakdown": dict(analytics_counts),
        "analytics_error_types": dict(analytics_error_types),
        "series": [{"date": label, "reach": reach_by_day.get(label, 0), "engagement": engagement_by_day.get(label, 0)} for label in labels],
        "top_posts": top_posts[:5],
        "best_posting_time": f"{best_hour:02d}:00" if best_hour is not None else "",
        "content_performance": content_performance[:6],
        "warnings": (warnings or [])[:20],
        "cached": True,
    }


def sync_facebook_aggregate_stats(days: int = 7, max_pages: int = 25) -> dict[str, Any]:
    cache_key = f"{max(1, min(days, 30))}:{max(1, min(max_pages, 100))}"
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    payload = _facebook_stats_from_cached_posts(days, len(pages), ["Stats recomputed from cached Facebook posts. Use Posts sync to refresh Graph metrics."])
    _upsert_facebook_stats(cache_key, payload)
    return payload
