from __future__ import annotations

import json
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
        "cover_url": item.get("cover_url", ""),
        "tasks": item.get("tasks") or [],
        "status": item.get("status", "connected"),
        "token_prefix": _mask_token(str(item.get("page_access_token") or "")),
        "connected_at": item.get("connected_at", ""),
        "updated_at": item.get("updated_at", ""),
        "expires_in": item.get("expires_in"),
    }


def list_facebook_pages() -> list[dict[str, Any]]:
    return [_public_page(item) for item in _list_facebook_page_records()]


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
            "engagement": sum(_safe_int(post.get("engagement")) for post in selected),
            "comments": sum(_safe_int(post.get("comments")) for post in selected),
            "shares": sum(_safe_int(post.get("shares")) for post in selected),
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
    metrics = {
        "reach": 0,
        "impressions": 0,
        "engagement": 0,
        "clicks": 0,
        "comments": 0,
        "reactions": 0,
        "shares": 0,
    }
    warnings: list[str] = []
    try:
        response = client.get(
            f"{base_url}/{post_id}/insights",
            params={
                "metric": "post_impressions_unique,post_impressions,post_engaged_users,post_clicks",
                "access_token": token,
            },
        )
        response.raise_for_status()
        payload = response.json()
        metrics["reach"] = _post_insight_total(payload, "post_impressions_unique")
        metrics["impressions"] = _post_insight_total(payload, "post_impressions")
        metrics["engagement"] = _post_insight_total(payload, "post_engaged_users")
        metrics["clicks"] = _post_insight_total(payload, "post_clicks")
    except httpx.HTTPError as error:
        warnings.append(f"{post_id}: insights unavailable - {_graph_error_message(error)}")

    try:
        response = client.get(
            f"{base_url}/{post_id}/comments",
            params={"summary": "true", "limit": 0, "access_token": token},
        )
        response.raise_for_status()
        metrics["comments"] = _safe_int(((response.json().get("summary") or {}).get("total_count")))
    except httpx.HTTPError as error:
        object_id = post_id.split("_", 1)[1] if "_" in post_id else ""
        if object_id:
            try:
                response = client.get(
                    f"{base_url}/{object_id}/comments",
                    params={"summary": "true", "limit": 0, "access_token": token},
                )
                response.raise_for_status()
                metrics["comments"] = _safe_int(((response.json().get("summary") or {}).get("total_count")))
            except httpx.HTTPError as fallback_error:
                warnings.append(f"{post_id}: comments count unavailable - {_graph_error_message(fallback_error)}")
        else:
            warnings.append(f"{post_id}: comments count unavailable - {_graph_error_message(error)}")

    try:
        response = client.get(
            f"{base_url}/{post_id}/reactions",
            params={"summary": "true", "limit": 0, "access_token": token},
        )
        response.raise_for_status()
        metrics["reactions"] = _safe_int(((response.json().get("summary") or {}).get("total_count")))
    except httpx.HTTPError as error:
        warnings.append(f"{post_id}: reactions count unavailable - {_graph_error_message(error)}")

    return metrics, warnings


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

    with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
        for page in pages:
            remaining = limit
            after = None
            try:
                while remaining > 0:
                    params = {
                        "fields": "id,message,created_time,permalink_url",
                        "limit": min(25, remaining),
                        "access_token": page["page_access_token"],
                    }
                    if after:
                        params["after"] = after
                    response = client.get(f"{base_url}/{page['page_id']}/posts", params=params)
                    response.raise_for_status()
                    posts_payload = response.json()
                    batch = posts_payload.get("data") or []
                    if not batch:
                        break
                    for post in batch:
                        created_time = str(post.get("created_time") or "")
                        created_dt = _parse_graph_time(created_time)
                        post_id = str(post.get("id") or "")
                        analytics, analytics_warnings = _fetch_post_analytics(client, base_url, post_id, str(page["page_access_token"]))
                        warnings.extend(analytics_warnings)
                        post_record = {
                            "post_id": post_id,
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
                            "posted_7d": bool(created_dt and created_dt >= since_7d),
                        }
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
    mime_type = str(raw.get("mime_type") or image_data.get("mime_type") or video_data.get("mime_type") or "")
    image_url = str(image_data.get("url") or image_data.get("preview_url") or "")
    video_url = str(video_data.get("url") or video_data.get("preview_url") or "")
    file_url = str(raw.get("file_url") or image_url or video_url or raw.get("url") or "")
    attachment_type = "file"
    if mime_type.startswith("image/") or image_url:
        attachment_type = "image"
    elif mime_type.startswith("video/") or video_url:
        attachment_type = "video"
    elif mime_type.startswith("audio/"):
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


def _fetch_message_extras(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    message_id: str,
) -> dict[str, Any]:
    extras: dict[str, Any] = {"attachments": [], "shares": {}, "sticker": {}}
    if not message_id:
        return extras
    try:
        response = client.get(
            f"{base_url}/{message_id}",
            params={"fields": "attachments,shares,sticker", "access_token": access_token},
        )
        response.raise_for_status()
        payload = response.json()
        extras["shares"] = payload.get("shares") or {}
        extras["sticker"] = payload.get("sticker") or {}
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
        response = client.get(f"{base_url}/{message_id}/attachments", params={"access_token": access_token})
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
        fallback_label = ""
        if not message.get("message"):
            if attachments:
                fallback_label = "Đã gửi tệp đính kèm"
            elif message.get("shares"):
                fallback_label = "Đã chia sẻ liên kết"
            elif message.get("sticker"):
                fallback_label = "Đã gửi sticker"
            else:
                fallback_label = "Tin nhắn đặc biệt"
        messages.append(
            {
                "message_id": str(message.get("id") or ""),
                "message": str(message.get("message") or ""),
                "created_time": str(message.get("created_time") or ""),
                "from_id": sender_id,
                "from_name": _message_participant_name(sender),
                "to_id": str(recipient.get("id") or ""),
                "to_name": _message_participant_name(recipient),
                "direction": "outbound" if sender_id == page_id else "inbound",
                "attachments": attachments,
                "fallback_label": fallback_label,
            }
        )
    messages.sort(key=lambda item: item.get("created_time") or "")
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


def facebook_conversations(limit: int = 50, max_pages: int = 25) -> dict[str, Any]:
    conversations = _list_cached_facebook_conversations(limit)
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    warnings = [] if conversations else ["No cached Facebook conversations yet. Run sync to fetch inbox from Graph API."]
    return {
        "total": len(conversations),
        "page_count": len(pages),
        "conversations": conversations,
        "warnings": warnings[:20],
    }


def sync_facebook_conversations(limit: int = 50, max_pages: int = 25) -> dict[str, Any]:
    settings = get_settings()
    limit = max(1, min(limit, 100))
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    conversations: list[dict[str, Any]] = []
    warnings: list[str] = []
    fields = "id,snippet,updated_time,unread_count,message_count,participants,messages.limit(30){id,message,created_time,from,to,attachments{mime_type,name,file_url,image_data,video_data}}"

    with httpx.Client(timeout=httpx.Timeout(12.0, connect=3.0)) as client:
        for page in pages:
            after = None
            fetched_for_page = 0
            try:
                while len(conversations) < limit:
                    params = {
                        "fields": fields,
                        "limit": min(25, limit - len(conversations)),
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
                        conversation = _normalize_conversation(page, item)
                        conversations.append(conversation)
                        fetched_for_page += 1
                        _upsert_facebook_conversation(conversation)
                    after = ((payload.get("paging") or {}).get("cursors") or {}).get("after")
                    if not after or fetched_for_page >= limit:
                        break
            except httpx.HTTPError as error:
                warnings.append(f"{page.get('name') or page.get('page_id')}: conversations unavailable - {_graph_error_message(error)}")
                continue
            if len(conversations) >= limit:
                break

    conversations.sort(key=lambda item: item.get("updated_time") or "", reverse=True)
    return {
        "total": len(conversations),
        "page_count": len(pages),
        "conversations": conversations[:limit],
        "warnings": warnings[:20],
    }


def send_facebook_message(conversation_id: str, message: str) -> dict[str, Any]:
    conversation_id = (conversation_id or "").strip()
    message = (message or "").strip()
    if not conversation_id:
        raise RuntimeError("conversation_id is required.")
    if not message:
        raise RuntimeError("Message is required.")
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
    with httpx.Client(timeout=httpx.Timeout(12.0, connect=3.0)) as client:
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

    sent_message = {
        "message_id": str(payload.get("message_id") or ""),
        "message": message,
        "created_time": _now(),
        "from_id": page_id,
        "from_name": conversation.get("page_name") or "Page",
        "to_id": customer_id,
        "to_name": conversation.get("customer_name") or "Facebook User",
        "direction": "outbound",
    }
    conversation["messages"] = (conversation.get("messages") or []) + [sent_message]
    conversation["snippet"] = message
    conversation["updated_time"] = sent_message["created_time"]
    conversation["status"] = "open"
    _upsert_facebook_conversation(conversation)
    return {"sent": True, "conversation_id": conversation_id, "message_id": sent_message["message_id"]}


def facebook_aggregate_stats(days: int = 7, max_pages: int = 25) -> dict[str, Any]:
    cache_key = f"{max(1, min(days, 30))}:{max(1, min(max_pages, 100))}"
    cached = _get_cached_facebook_stats(cache_key)
    if cached:
        cached["cached"] = True
        return cached
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    return _empty_facebook_stats(days, len(pages), ["No cached Facebook stats yet. Run sync to fetch stats from Graph API."])


def sync_facebook_aggregate_stats(days: int = 7, max_pages: int = 25) -> dict[str, Any]:
    cache_key = f"{max(1, min(days, 30))}:{max(1, min(max_pages, 100))}"

    settings = get_settings()
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")][: max(1, min(max_pages, 100))]
    base_url = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=max(1, min(days, 30)))
    warnings: list[str] = []
    reach_by_day: defaultdict[str, int] = defaultdict(int)
    engagement_by_day: defaultdict[str, int] = defaultdict(int)
    fan_count = 0
    comments = 0
    shares = 0
    post_count = 0
    top_posts: list[dict[str, Any]] = []
    content_types: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"posts": 0, "reach": 0})
    hour_buckets: defaultdict[int, int] = defaultdict(int)

    with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        for page in pages:
            insight_values, metric_warnings = _fetch_metrics_payload(
                client,
                base_url,
                page,
                ["page_impressions_unique", "page_post_engagements"],
                since,
                now,
            )
            warnings.extend(metric_warnings)
            for metric, target in [
                ("page_impressions_unique", reach_by_day),
                ("page_post_engagements", engagement_by_day),
            ]:
                for item in insight_values.get(metric, []):
                    key = _date_key(item.get("end_time"))
                    if key:
                        target[key] += _safe_int(item.get("value"))

            try:
                page_response = client.get(
                    f"{base_url}/{page['page_id']}",
                    params={
                        "fields": "followers_count,fan_count",
                        "access_token": page["page_access_token"],
                    },
                )
                page_response.raise_for_status()
                page_payload = page_response.json()
                fan_count += _safe_int(page_payload.get("followers_count") or page_payload.get("fan_count"))
            except httpx.HTTPError as error:
                warnings.append(f"{page.get('name') or page.get('page_id')}: follower count unavailable - {_graph_error_message(error)}")

            try:
                posts_response = client.get(
                    f"{base_url}/{page['page_id']}/posts",
                    params={
                        "fields": "id,message,created_time,status_type,type,insights.metric(post_impressions_unique,post_engaged_users)",
                        "limit": 10,
                        "access_token": page["page_access_token"],
                    },
                )
                posts_response.raise_for_status()
                for post in posts_response.json().get("data") or []:
                    post_count += 1
                    post_comments = 0
                    post_shares = 0
                    post_reach = _post_insight_total(post.get("insights"), "post_impressions_unique")
                    post_engagement = _post_insight_total(post.get("insights"), "post_engaged_users")
                    comments += post_comments
                    shares += post_shares
                    content_type = str(post.get("type") or post.get("status_type") or "unknown").replace("_", " ").title()
                    content_types[content_type]["posts"] += 1
                    content_types[content_type]["reach"] += post_reach
                    created_time = str(post.get("created_time") or "")
                    if "T" in created_time:
                        try:
                            hour_buckets[datetime.fromisoformat(created_time.replace("Z", "+00:00")).hour] += post_engagement
                        except ValueError:
                            pass
                    top_posts.append(
                        {
                            "page_name": page.get("name") or "",
                            "message": (post.get("message") or "Untitled post")[:120],
                            "reach": post_reach,
                            "engagement": post_engagement,
                            "comments": post_comments,
                            "shares": post_shares,
                        }
                    )
            except httpx.HTTPError as error:
                warnings.append(f"{page.get('name') or page.get('page_id')}: posts unavailable - {_graph_error_message(error)}")

    reach_total = sum(reach_by_day.values())
    engagement_total = sum(engagement_by_day.values())
    ctr = round((engagement_total / reach_total) * 100, 2) if reach_total else 0.0
    labels = [(since + timedelta(days=index + 1)).date().isoformat() for index in range(max(1, min(days, 30)))]
    best_hour = max(hour_buckets.items(), key=lambda item: item[1])[0] if hour_buckets else None
    content_performance = []
    for label, data in content_types.items():
        posts = data["posts"]
        content_performance.append(
            {
                "type": label,
                "posts": posts,
                "avg_reach": round(data["reach"] / posts) if posts else 0,
            }
        )
    content_performance.sort(key=lambda item: item["avg_reach"], reverse=True)
    top_posts.sort(key=lambda item: item["reach"] + item["engagement"], reverse=True)

    payload = {
        "days": max(1, min(days, 30)),
        "page_count": len(pages),
        "totals": {
            "reach": reach_total,
            "engagement": engagement_total,
            "likes": fan_count,
            "shares": shares,
            "comments": comments,
            "ctr": ctr,
            "posts": post_count,
        },
        "series": [
            {
                "date": label,
                "reach": reach_by_day.get(label, 0),
                "engagement": engagement_by_day.get(label, 0),
            }
            for label in labels
        ],
        "top_posts": top_posts[:5],
        "best_posting_time": f"{best_hour:02d}:00" if best_hour is not None else "",
        "content_performance": content_performance[:6],
        "warnings": warnings[:20],
        "cached": False,
    }
    _upsert_facebook_stats(cache_key, payload)
    return payload
