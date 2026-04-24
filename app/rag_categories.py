from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


DATA_PATH = Path("data/rag_categories.json")


def _ensure_store() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        DATA_PATH.write_text("[]", encoding="utf-8")


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def _slugify(value: str) -> str:
    lowered = _strip_accents(value).lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-")


def list_categories() -> list[str]:
    _ensure_store()
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = []
    if not isinstance(payload, list):
        payload = []
    categories = [str(item).strip() for item in payload if str(item).strip()]
    categories.sort(key=lambda item: item.lower())
    return categories


def create_category(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip(" -–|,.;")
    if not cleaned:
        raise ValueError("Category name is required")
    slug = _slugify(cleaned)
    if not slug:
        raise ValueError("Category name is invalid")
    categories = list_categories()
    existing = next((item for item in categories if _slugify(item) == slug), None)
    if existing:
        return existing
    categories.append(cleaned)
    categories.sort(key=lambda item: item.lower())
    _ensure_store()
    DATA_PATH.write_text(json.dumps(categories, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleaned
