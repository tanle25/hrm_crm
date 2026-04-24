from __future__ import annotations

import re
import unicodedata
from html import unescape
from urllib.parse import urlparse


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def clean_phrase(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip(" .,:;|-")


def _host_terms(host: str) -> list[str]:
    host = host.lower().strip()
    host = host[4:] if host.startswith("www.") else host
    terms: list[str] = []
    if not host:
        return terms
    terms.append(host)
    parts = [part for part in re.split(r"[^a-z0-9-]+", host) if part and part not in {"com", "vn", "net", "org"}]
    terms.extend(parts)
    for part in parts:
        # Dynamic brand variant derived from source domains, avoiding concrete
        # source-brand hard-codes while still blocking domain-derived mentions.
        if part.startswith("tra") and len(part) > 4:
            suffix = part[3:]
            terms.append(f"tra {suffix}")
            terms.append(f"trà {suffix}")
    return terms


def source_terms_from_metadata(metadata: dict | None, fallback_url: str = "") -> list[str]:
    metadata = metadata or {}
    source_url = str(metadata.get("url") or fallback_url or "")
    candidates: list[str] = []
    if source_url:
        candidates.extend(_host_terms(urlparse(source_url).netloc))
    for key in ["sitename", "author", "site_name", "publisher"]:
        value = metadata.get(key)
        if isinstance(value, str):
            candidates.append(value)

    terms: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        term = clean_phrase(str(candidate)).lower()
        term = re.sub(r"\b(com|vn|net|org|www)\b", " ", term)
        term = re.sub(r"\s+", " ", term).strip(" .,:;|-")
        if len(term) < 4:
            continue
        compact = re.sub(r"[^a-z0-9à-ỹ]+", "", term)
        ascii_compact = re.sub(r"[^a-z0-9]+", "", strip_accents(term).lower())
        for item in {term, compact, ascii_compact}:
            if len(item) >= 4 and item not in seen:
                seen.add(item)
                terms.append(item)
    return terms


def clean_source_text(text: str, metadata: dict | None, fallback_url: str = "", replacement: str = "thông tin sản phẩm") -> str:
    cleaned = re.sub(r"https?://\S+", "", text or "", flags=re.IGNORECASE)
    for term in source_terms_from_metadata(metadata, fallback_url):
        if re.fullmatch(r"[a-z0-9-]+", term):
            pattern = rf"\b{re.escape(term)}\b"
        else:
            pattern = _accent_insensitive_pattern(term)
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(website|url)\s+nguồn\b", replacement, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bnguồn\s+tham\s+khảo\b", "thông tin tham khảo", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_source_object(value: object, metadata: dict | None, fallback_url: str = "", replacement: str = "thông tin sản phẩm") -> object:
    if isinstance(value, str):
        return clean_source_text(value, metadata, fallback_url, replacement)
    if isinstance(value, list):
        return [clean_source_object(item, metadata, fallback_url, replacement) for item in value]
    if isinstance(value, dict):
        blocked_keys = {"url", "canonical_url", "source_url", "sitename", "author", "publisher", "site_name"}
        return {
            key: clean_source_object(item, metadata, fallback_url, replacement)
            for key, item in value.items()
            if str(key).lower() not in blocked_keys
        }
    return value


def contains_source_term(text: str, metadata: dict | None, fallback_url: str = "") -> bool:
    lowered = (text or "").lower()
    ascii_lowered = strip_accents(lowered)
    return any(term and (term in lowered or strip_accents(term) in ascii_lowered) for term in source_terms_from_metadata(metadata, fallback_url))


def _accent_insensitive_pattern(term: str) -> str:
    classes = {
        "a": "[aàáảãạăằắẳẵặâầấẩẫậ]",
        "e": "[eèéẻẽẹêềếểễệ]",
        "i": "[iìíỉĩị]",
        "o": "[oòóỏõọôồốổỗộơờớởỡợ]",
        "u": "[uùúủũụưừứửữự]",
        "y": "[yỳýỷỹỵ]",
        "d": "[dđ]",
    }
    pieces: list[str] = []
    for char in strip_accents(term.lower()):
        if char.isspace():
            pieces.append(r"\s+")
        elif char in classes:
            pieces.append(classes[char])
        else:
            pieces.append(re.escape(char))
    return "".join(pieces)
