from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def postgres_available() -> bool:
    settings = get_settings()
    return bool(settings.postgres_url and psycopg is not None)


def get_connection():
    settings = get_settings()
    if not settings.postgres_url or psycopg is None:
        return None
    return psycopg.connect(settings.postgres_url, autocommit=True)


def serialize_json(value: dict | list) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def init_schema() -> None:
    conn = get_connection()
    if conn is None:
        return
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sites (
                site_id TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shopee_products (
                item_id TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_urls (
                url_hash TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dlq_entries (
                job_id TEXT PRIMARY KEY,
                failed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_tokens (
                token_id TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_pages (
                page_id TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_page_groups (
                group_name TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_posts (
                post_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL DEFAULT '',
                created_time TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_comments (
                comment_id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL DEFAULT '',
                page_id TEXT NOT NULL DEFAULT '',
                created_time TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_stats (
                stat_key TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_conversations (
                conversation_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL DEFAULT '',
                updated_time TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL DEFAULT '',
                page_id TEXT NOT NULL DEFAULT '',
                customer_id TEXT NOT NULL DEFAULT '',
                created_time TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_content_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'draft',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_reel_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_sync_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facebook_slash_commands (
                command TEXT PRIMARY KEY,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS flowkit_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'processing',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data JSONB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_meta (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL DEFAULT 0
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_facebook_conversations_updated_time
            ON facebook_conversations (updated_time DESC NULLS LAST, updated_at DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_facebook_messages_conversation_created
            ON facebook_messages (conversation_id, created_time DESC NULLS LAST, updated_at DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_facebook_posts_created_time
            ON facebook_posts (created_time DESC NULLS LAST, updated_at DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_facebook_content_jobs_status_updated
            ON facebook_content_jobs (status, updated_at DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_facebook_reel_jobs_status_updated
            ON facebook_reel_jobs (status, updated_at DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_facebook_sync_jobs_status_updated
            ON facebook_sync_jobs (status, updated_at DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_flowkit_jobs_status_updated
            ON flowkit_jobs (status, updated_at DESC);
            """
        )
        cur.execute("INSERT INTO job_meta (key, value) VALUES ('jobs_version', 0) ON CONFLICT (key) DO NOTHING;")


def migrate_local_state() -> dict[str, int]:
    conn = get_connection()
    if conn is None:
        return {"sites": 0, "jobs": 0, "processed_urls": 0, "dlq_entries": 0}

    migrated = {"sites": 0, "jobs": 0, "processed_urls": 0, "dlq_entries": 0}
    data_dir = Path("data")
    sites_path = data_dir / "sites.json"
    jobs_path = data_dir / "job_store.json"

    with conn, conn.cursor() as cur:
        if sites_path.exists():
            try:
                sites = json.loads(sites_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                sites = []
            if isinstance(sites, list):
                for item in sites:
                    if not isinstance(item, dict) or not item.get("site_id"):
                        continue
                    cur.execute(
                        """
                        INSERT INTO sites (site_id, updated_at, data)
                        VALUES (%s, NOW(), %s::jsonb)
                        ON CONFLICT (site_id) DO NOTHING
                        """,
                        (str(item["site_id"]), serialize_json(item)),
                    )
                    migrated["sites"] += cur.rowcount

        if jobs_path.exists():
            try:
                payload = json.loads(jobs_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}

            jobs = payload.get("jobs") or {}
            if isinstance(jobs, dict):
                for job_id, item in jobs.items():
                    if not isinstance(item, dict):
                        continue
                    cur.execute(
                        """
                        INSERT INTO jobs (job_id, updated_at, data)
                        VALUES (%s, NOW(), %s::jsonb)
                        ON CONFLICT (job_id) DO NOTHING
                        """,
                        (str(job_id), serialize_json(item)),
                    )
                    migrated["jobs"] += cur.rowcount

            processed_urls = payload.get("processed_url_hashes") or {}
            if isinstance(processed_urls, dict):
                for url_hash, item in processed_urls.items():
                    if not isinstance(item, dict):
                        continue
                    cur.execute(
                        """
                        INSERT INTO processed_urls (url_hash, updated_at, data)
                        VALUES (%s, NOW(), %s::jsonb)
                        ON CONFLICT (url_hash) DO NOTHING
                        """,
                        (str(url_hash), serialize_json(item)),
                    )
                    migrated["processed_urls"] += cur.rowcount

            dlq_entries = payload.get("dlq") or []
            if isinstance(dlq_entries, list):
                for item in dlq_entries:
                    if not isinstance(item, dict) or not item.get("job_id"):
                        continue
                    cur.execute(
                        """
                        INSERT INTO dlq_entries (job_id, failed_at, data)
                        VALUES (%s, NOW(), %s::jsonb)
                        ON CONFLICT (job_id) DO NOTHING
                        """,
                        (str(item["job_id"]), serialize_json(item)),
                    )
                    migrated["dlq_entries"] += cur.rowcount

        total = sum(migrated.values())
        if total:
            cur.execute(
                "UPDATE job_meta SET value = value + %s WHERE key = 'jobs_version'",
                (migrated["jobs"] + migrated["dlq_entries"],),
            )

    return migrated
