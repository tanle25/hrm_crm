from __future__ import annotations

import hashlib

from app.job_store import get_processed_url


def run(url: str) -> dict:
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    existing = get_processed_url(url_hash)
    if existing:
        return {
            "is_duplicate": True,
            "url_hash": url_hash,
            "existing_post_id": existing.get("woo_post_id"),
        }
    return {"is_duplicate": False, "url_hash": url_hash, "existing_post_id": None}
