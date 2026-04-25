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


def _fetch_metric_series(
    client: httpx.Client,
    base_url: str,
    page: dict[str, Any],
    metric: str,
    since: datetime,
    until: datetime,
) -> tuple[list[dict[str, Any]], str | None]:
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
        return _insight_values(response.json(), metric), None
    except httpx.HTTPError as error:
        return [], f"{page.get('name') or page.get('page_id')}: {metric} unavailable ({error})"


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


def facebook_aggregate_stats(days: int = 7) -> dict[str, Any]:
    settings = get_settings()
    pages = [page for page in _list_facebook_page_records() if page.get("page_access_token")]
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

    with httpx.Client(timeout=30) as client:
        for page in pages:
            for metric, target in [
                ("page_impressions_unique", reach_by_day),
                ("page_post_engagements", engagement_by_day),
            ]:
                values, warning = _fetch_metric_series(client, base_url, page, metric, since, now)
                if warning:
                    warnings.append(warning)
                    continue
                for item in values:
                    key = _date_key(item.get("end_time"))
                    if key:
                        target[key] += _safe_int(item.get("value"))

            fan_values, warning = _fetch_metric_series(client, base_url, page, "page_fans", since, now)
            if warning:
                warnings.append(warning)
            elif fan_values:
                fan_count += _safe_int(fan_values[-1].get("value"))

            try:
                posts_response = client.get(
                    f"{base_url}/{page['page_id']}/posts",
                    params={
                        "fields": "id,message,created_time,status_type,type,shares,comments.summary(true),reactions.summary(true),insights.metric(post_impressions_unique,post_engaged_users)",
                        "limit": 25,
                        "access_token": page["page_access_token"],
                    },
                )
                posts_response.raise_for_status()
                for post in posts_response.json().get("data") or []:
                    post_count += 1
                    post_comments = _safe_int(((post.get("comments") or {}).get("summary") or {}).get("total_count"))
                    post_reactions = _safe_int(((post.get("reactions") or {}).get("summary") or {}).get("total_count"))
                    post_shares = _safe_int((post.get("shares") or {}).get("count"))
                    post_reach = _post_insight_total(post.get("insights"), "post_impressions_unique")
                    post_engagement = _post_insight_total(post.get("insights"), "post_engaged_users") or post_reactions + post_comments + post_shares
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
                warnings.append(f"{page.get('name') or page.get('page_id')}: posts unavailable ({error})")

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

    return {
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
    }
