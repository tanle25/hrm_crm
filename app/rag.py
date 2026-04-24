from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

from app.agents import extractor, fetcher
from app.chroma import add_document, delete_documents, get_documents, search_documents
from app.llm import call_json


RAG_TAXONOMY_SYSTEM_PROMPT = """
Ban la taxonomy curator cho kho kien thuc RAG.
Muc tieu:
- Chi giu 1 primary_category gon, uu tien manual category do nguoi dung nhap.
- Tu dong sinh subcategories nho hon, knowledge_types va tags phu hop voi noi dung that.
- Khong tao subcategory qua giong nhau. Neu da co subcategory gan nghia trong danh sach ton tai, hay dung lai ten cu.
- Ten subcategory phai gon, tu nhien, de tai su dung cho nhieu URL cung nhom.
- knowledge_types la nhan chuc nang nhu: product_knowledge, brewing_guide, storage_guide, origin_story, flavor_profile, material_care, styling_guide, gifting_context.

Tra ve JSON hop le:
{
  "primary_category": "...",
  "subcategories": ["..."],
  "knowledge_types": ["..."],
  "tags": ["..."],
  "usage_intents": ["..."]
}
Khong giai thich.
""".strip()


RAG_SELECTION_SYSTEM_PROMPT = """
Ban la knowledge selector cho content pipeline.
Hay doc thong tin san pham/bai viet hien tai va danh sach knowledge chunks ung vien.
Chi chon nhung chunks that su giup bai viet tot hon.

Nguyen tac:
- Khong lay kien thuc cho co. Neu khong can, tra ve selected = [].
- Khong lay nhung thong tin trung lap voi source hien tai neu no khong them gia tri moi.
- Voi san pham, uu tien kien thuc nen co the giup nguoi mua hieu hon, chon dung hon, dung dung hon.
- LLM tu quyet dinh khi nao nen dua cac kieu kien thuc nhu cach pha, bao quan, nguon goc, huong vi, boi canh su dung.
- Khong ep buoc moi san pham phai co huong dan pha tra.
- Toi da 6 muc.

Tra ve JSON hop le:
{
  "selected": [
    {
      "candidate_id": "...",
      "fact": "...",
      "integration_hint": "...",
      "reason": "..."
    }
  ]
}
Khong giai thich.
""".strip()


RAG_SEARCH_PLAN_SYSTEM_PROMPT = """
Ban la query planner cho knowledge RAG.
Tu thong tin bai viet/san pham hien tai, hay xac dinh:
- primary_category nao trong danh sach san co co kha nang phu hop nhat
- 2-4 search queries gon va sat nghia
- desired_knowledge_types neu co

Nguyen tac:
- Khong chon category neu khong co co so.
- Voi san pham, chi tim kien thuc giup noi dung tot hon, khong tim cho co.

Tra ve JSON hop le:
{
  "primary_category": "...",
  "queries": ["..."],
  "desired_knowledge_types": ["..."]
}
Khong giai thich.
""".strip()


def _canonical_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _normalize_list(values: list[str] | None) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" -–|,.;")
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
    return output


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def _slugify(value: str) -> str:
    lowered = _strip_accents(value).lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-")


def _slug_tokens(value: str) -> set[str]:
    return {token for token in _slugify(value).split("-") if token}


def _labels_equivalent(left: str, right: str) -> bool:
    left_slug = _slugify(left)
    right_slug = _slugify(right)
    if not left_slug or not right_slug:
        return False
    if left_slug == right_slug:
        return True
    left_tokens = _slug_tokens(left)
    right_tokens = _slug_tokens(right)
    if left_tokens and left_tokens == right_tokens:
        return True
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens), 1)
    if overlap >= 0.8 and abs(len(left_tokens) - len(right_tokens)) <= 1:
        return True
    return SequenceMatcher(None, left_slug, right_slug).ratio() >= 0.9


def _canonicalize_labels(proposed: list[str], existing: list[str], limit: int = 6) -> list[str]:
    canonical: list[str] = []
    seen: set[str] = set()
    pool = [item for item in existing if item]
    for label in _normalize_list(proposed):
        chosen = next((item for item in pool if _labels_equivalent(label, item)), label)
        key = _slugify(chosen)
        if not key or key in seen:
            continue
        seen.add(key)
        canonical.append(chosen)
        if chosen not in pool:
            pool.append(chosen)
        if len(canonical) >= limit:
            break
    return canonical


def _csv_to_list(value: str | None) -> list[str]:
    if not value:
        return []
    return _normalize_list([part for part in str(value).split(",") if part.strip()])


def _existing_taxonomy(primary_category: str | None = None) -> dict[str, list[str]]:
    docs = get_documents(where={"primary_category": primary_category}) if primary_category else get_documents()
    subcategories: list[str] = []
    knowledge_types: list[str] = []
    usage_intents: list[str] = []
    primary_categories: list[str] = []
    for item in docs:
        metadata = item.get("metadata", {})
        primary = str(metadata.get("primary_category") or "").strip()
        if primary:
            primary_categories.append(primary)
        subcategories.extend(_csv_to_list(metadata.get("subcategories")))
        knowledge_types.extend(_csv_to_list(metadata.get("knowledge_types")))
        usage_intents.extend(_csv_to_list(metadata.get("usage_intents")))
    return {
        "primary_categories": _canonicalize_labels(primary_categories, []),
        "subcategories": _canonicalize_labels(subcategories, []),
        "knowledge_types": _canonicalize_labels(knowledge_types, []),
        "usage_intents": _canonicalize_labels(usage_intents, []),
    }


def _is_useful_fact(value: str) -> bool:
    cleaned = re.sub(r"\s+", " ", value or "").strip(" -–|,.;:")
    if len(cleaned) < 8:
        return False
    words = cleaned.split()
    if len(words) == 1 and not re.search(r"\d", cleaned):
        return False
    if cleaned.lower().startswith("sản phẩm gồm"):
        return False
    return True


def _source_id(url: str) -> str:
    return hashlib.sha256(_canonical_url(url).encode("utf-8")).hexdigest()[:16]


def _chunk_id(source_id: str, chunk_kind: str, index: int, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"rag_{source_id}_{chunk_kind}_{index}_{digest}"


def _paragraph_chunks(text: str, max_chars: int = 700) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}|(?<=[.!?])\s+(?=[A-ZÀ-Ỵ])", text or "") if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if not current:
            current = paragraph
            continue
        if len(current) + len(paragraph) + 1 <= max_chars:
            current = f"{current} {paragraph}"
            continue
        chunks.append(current)
        current = paragraph
    if current:
        chunks.append(current)
    return chunks[:12]


def _specs_summary(specs: dict[str, Any]) -> str:
    if not specs:
        return ""
    parts = []
    ordered_keys = [
        "package_sizes_text",
        "packets_per_box",
        "grams_per_packet",
        "component_count",
        "total_packets",
        "total_weight_grams",
        "box_name",
        "box_material",
        "audience_hint",
    ]
    for key in ordered_keys:
        value = specs.get(key)
        if value in ("", None) or value == []:
            continue
        parts.append(f"{key}: {value}")
    return "; ".join(parts)


def _faq_chunks(faq_items: list[dict]) -> list[str]:
    chunks = []
    for item in faq_items[:8]:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if (
            question
            and answer
            and len(answer) <= 220
            and answer.lower().count("trà ") < 5
            and answer.count(",") < 8
        ):
            chunks.append(f"Hỏi: {question} Đáp: {answer}")
    return chunks


def _heuristic_taxonomy(
    fetched: dict,
    extracted: dict,
    manual_categories: list[str],
    manual_tags: list[str],
    note: str | None,
) -> dict:
    metadata = fetched.get("metadata", {})
    title = str(fetched.get("title") or "")
    source_type = str(metadata.get("source_type") or "")
    product_kind = str(metadata.get("product_kind") or "")
    primary_category = manual_categories[0] if manual_categories else source_type or "general"
    lower = " ".join(
        [
            title,
            note or "",
            " ".join(str(item) for item in extracted.get("key_points", [])[:4]),
            " ".join(str(item) for item in extracted.get("important_facts", [])[:6]),
            str((extracted.get("product_specs") or {}).get("package_sizes_text") or ""),
        ]
    ).lower()

    subcategories: list[str] = []
    if "trà xanh" in lower:
        subcategories.append("trà xanh")
    if "hồng trà" in lower or "hong tra" in _slugify(lower):
        subcategories.append("hồng trà")
    if "oolong" in lower:
        subcategories.append("trà oolong")
    if "shan tuyết" in lower or "shan" in lower:
        subcategories.append("shan tuyết")
    if "móc câu" in lower:
        subcategories.append("trà móc câu")
    if any(term in lower for term in ["suối giàng", "hà giang", "thái nguyên", "yên bái"]):
        for term in ["suối giàng", "hà giang", "thái nguyên", "yên bái"]:
            if term in lower:
                subcategories.append(term)
    if any(term in lower for term in ["cách pha", "pha trà", "nhiệt độ nước", "thời gian hãm"]):
        subcategories.append("cách pha")
    if any(term in lower for term in ["bảo quản", "giữ hương", "đậy kín"]):
        subcategories.append("bảo quản")
    if source_type == "product":
        subcategories.append("kiến thức sản phẩm")

    knowledge_types: list[str] = []
    if source_type == "product":
        knowledge_types.append("product_knowledge")
    if any(term in lower for term in ["cách pha", "pha trà", "nhiệt độ nước", "hãm"]):
        knowledge_types.append("brewing_guide")
    if any(term in lower for term in ["bảo quản", "đậy kín", "tránh ẩm", "tránh nắng"]):
        knowledge_types.append("storage_guide")
    if any(term in lower for term in ["nguồn gốc", "vùng", "suối giàng", "hà giang", "thái nguyên", "yên bái"]):
        knowledge_types.append("origin_story")
    if any(term in lower for term in ["hương", "vị", "hậu ngọt", "nước trà", "cánh trà"]):
        knowledge_types.append("flavor_profile")
    if not knowledge_types:
        knowledge_types.append("reference_knowledge")

    usage_intents: list[str] = []
    if source_type == "product":
        usage_intents.append("support_product_copy")
    if "pha" in lower:
        usage_intents.append("guide_usage")
    if "bảo quản" in lower:
        usage_intents.append("post_purchase_help")
    if any(term in lower for term in ["hương", "vị", "trải nghiệm"]):
        usage_intents.append("shape_buying_decision")

    tags = _normalize_list(
        manual_tags
        + extracted.get("product_attributes", [])
        + extracted.get("entities", {}).get("products", [])
        + subcategories
    )
    return {
        "primary_category": primary_category,
        "subcategories": subcategories[:6],
        "knowledge_types": knowledge_types[:4],
        "tags": tags[:10],
        "usage_intents": usage_intents[:4],
    }


def _classify_taxonomy(
    fetched: dict,
    extracted: dict,
    manual_categories: list[str],
    manual_tags: list[str],
    note: str | None,
) -> dict:
    metadata = fetched.get("metadata", {})
    primary_hint = manual_categories[0] if manual_categories else ""
    existing = _existing_taxonomy(primary_hint or None)
    fallback = _heuristic_taxonomy(fetched, extracted, manual_categories, manual_tags, note)
    prompt = (
        f"Title: {fetched.get('title')}\n"
        f"Metadata: {metadata}\n"
        f"Manual categories: {manual_categories}\n"
        f"Manual tags: {manual_tags}\n"
        f"Note: {note or ''}\n"
        f"Extracted key points: {extracted.get('key_points', [])[:4]}\n"
        f"Extracted facts: {extracted.get('important_facts', [])[:6]}\n"
        f"Product use cases: {extracted.get('product_use_cases', [])[:4]}\n"
        f"Product attributes: {extracted.get('product_attributes', [])[:6]}\n"
        f"Product specs: {extracted.get('product_specs', {})}\n"
        f"Existing primary categories: {existing['primary_categories'][:20]}\n"
        f"Existing subcategories in same domain: {existing['subcategories'][:60]}\n"
        f"Existing knowledge types: {existing['knowledge_types'][:20]}\n"
        f"Existing usage intents: {existing['usage_intents'][:20]}\n"
    )
    data = call_json("knowledge", RAG_TAXONOMY_SYSTEM_PROMPT, prompt, fallback=fallback, max_tokens=700)
    primary_category = str(data.get("primary_category") or fallback["primary_category"]).strip() or fallback["primary_category"]
    if manual_categories:
        primary_category = manual_categories[0]
    subcategories = _canonicalize_labels(
        data.get("subcategories") if isinstance(data.get("subcategories"), list) else fallback["subcategories"],
        existing["subcategories"],
        limit=6,
    )
    knowledge_types = _canonicalize_labels(
        data.get("knowledge_types") if isinstance(data.get("knowledge_types"), list) else fallback["knowledge_types"],
        existing["knowledge_types"],
        limit=4,
    )
    usage_intents = _canonicalize_labels(
        data.get("usage_intents") if isinstance(data.get("usage_intents"), list) else fallback["usage_intents"],
        existing["usage_intents"],
        limit=4,
    )
    tags = _canonicalize_labels(
        _normalize_list((data.get("tags") if isinstance(data.get("tags"), list) else fallback["tags"]) + manual_tags),
        existing["subcategories"] + fallback["tags"],
        limit=10,
    )
    return {
        "primary_category": primary_category,
        "subcategories": subcategories,
        "knowledge_types": knowledge_types or fallback["knowledge_types"],
        "tags": tags,
        "usage_intents": usage_intents,
    }


def _knowledge_units(
    fetched: dict,
    extracted: dict,
    manual_categories: list[str],
    manual_tags: list[str],
    note: str | None,
) -> tuple[list[dict], dict]:
    metadata = fetched.get("metadata", {})
    title = fetched.get("title") or "Untitled"
    source_type = metadata.get("source_type", "")
    product_kind = metadata.get("product_kind", "")
    source_url = _canonical_url(metadata.get("url") or fetched.get("url") or "")
    source_id = _source_id(source_url)
    taxonomy = _classify_taxonomy(fetched, extracted, manual_categories, manual_tags, note)
    categories = _normalize_list(
        [taxonomy["primary_category"]] + manual_categories + [source_type, product_kind, metadata.get("sitename", "")]
    )
    tags = _normalize_list(taxonomy["tags"])
    base_meta = {
        "source_id": source_id,
        "source_url": source_url,
        "title": title,
        "site": metadata.get("sitename", ""),
        "language": metadata.get("language", "vi"),
        "source_type": source_type,
        "product_kind": product_kind,
        "primary_category": taxonomy["primary_category"],
        "categories": ", ".join(categories),
        "manual_categories": ", ".join(_normalize_list(manual_categories)),
        "subcategories": ", ".join(taxonomy["subcategories"]),
        "knowledge_types": ", ".join(taxonomy["knowledge_types"]),
        "usage_intents": ", ".join(taxonomy["usage_intents"]),
        "tags": ", ".join(tags),
        "ingested_at": datetime.utcnow().isoformat(),
    }
    units: list[dict] = []

    overview_parts = _normalize_list(
        [
            title,
            (metadata.get("product_hints") or {}).get("meta_description", ""),
            " ".join(extracted.get("key_points", [])[:3]),
            note or "",
        ]
    )
    if overview_parts:
        overview = ". ".join(overview_parts)
        units.append(
            {
                "id": _chunk_id(source_id, "overview", 0, overview),
                "document": overview,
                "metadata": {**base_meta, "chunk_kind": "overview", "chunk_index": 0},
            }
        )

    facts = [item for item in _normalize_list(extracted.get("important_facts", [])[:10]) if _is_useful_fact(item)]
    for idx, fact in enumerate(facts, start=1):
        units.append(
            {
                "id": _chunk_id(source_id, "fact", idx, fact),
                "document": fact,
                "metadata": {**base_meta, "chunk_kind": "fact", "chunk_index": idx},
            }
        )

    specs_summary = _specs_summary(extracted.get("product_specs") or {})
    if specs_summary:
        units.append(
            {
                "id": _chunk_id(source_id, "specs", 0, specs_summary),
                "document": specs_summary,
                "metadata": {**base_meta, "chunk_kind": "specs", "chunk_index": 0},
            }
        )

    for idx, item in enumerate([x for x in _normalize_list(extracted.get("product_use_cases", [])[:6]) if _is_useful_fact(x)], start=1):
        units.append(
            {
                "id": _chunk_id(source_id, "use_case", idx, item),
                "document": item,
                "metadata": {**base_meta, "chunk_kind": "use_case", "chunk_index": idx},
            }
        )

    for idx, item in enumerate([x for x in _normalize_list(extracted.get("buyer_objections", [])[:6]) if _is_useful_fact(x)], start=1):
        units.append(
            {
                "id": _chunk_id(source_id, "objection", idx, item),
                "document": item,
                "metadata": {**base_meta, "chunk_kind": "objection", "chunk_index": idx},
            }
        )

    for idx, item in enumerate(_faq_chunks(extracted.get("faq_items", [])), start=1):
        units.append(
            {
                "id": _chunk_id(source_id, "faq", idx, item),
                "document": item,
                "metadata": {**base_meta, "chunk_kind": "faq", "chunk_index": idx},
            }
        )

    content_chunks = _paragraph_chunks(fetched.get("clean_content", ""))
    for idx, chunk in enumerate(content_chunks, start=1):
        units.append(
            {
                "id": _chunk_id(source_id, "content", idx, chunk),
                "document": chunk,
                "metadata": {**base_meta, "chunk_kind": "content", "chunk_index": idx},
            }
        )

    return units, taxonomy


def ingest_url(
    url: str,
    manual_categories: list[str] | None = None,
    manual_tags: list[str] | None = None,
    note: str | None = None,
    force_reingest: bool = True,
) -> dict:
    manual_categories = _normalize_list(manual_categories or [])
    manual_tags = _normalize_list(manual_tags or [])
    fetched = fetcher.run(url)
    extracted = extractor.run(fetched["clean_content"], fetched.get("metadata", {}))
    source_url = _canonical_url(fetched.get("metadata", {}).get("url") or url)
    source_id = _source_id(source_url)

    existing = get_documents(where={"source_id": source_id})
    if existing and not force_reingest:
        first_meta = existing[0].get("metadata", {}) if existing else {}
        return {
            "status": "exists",
            "source_id": source_id,
            "source_url": source_url,
            "title": fetched.get("title"),
            "source_type": fetched.get("metadata", {}).get("source_type"),
            "product_kind": fetched.get("metadata", {}).get("product_kind"),
            "primary_category": first_meta.get("primary_category", ""),
            "subcategories": _csv_to_list(first_meta.get("subcategories")),
            "knowledge_types": _csv_to_list(first_meta.get("knowledge_types")),
            "usage_intents": _csv_to_list(first_meta.get("usage_intents")),
            "documents_count": len(existing),
            "document_ids": [item["id"] for item in existing[:20]],
        }
    if existing:
        delete_documents(where={"source_id": source_id})

    units, taxonomy = _knowledge_units(fetched, extracted, manual_categories, manual_tags, note)
    for unit in units:
        add_document(unit["document"], unit["metadata"], unit["id"])

    return {
        "status": "ingested",
        "source_id": source_id,
        "source_url": source_url,
        "title": fetched.get("title"),
        "source_type": fetched.get("metadata", {}).get("source_type"),
        "product_kind": fetched.get("metadata", {}).get("product_kind"),
        "primary_category": taxonomy["primary_category"],
        "categories": _normalize_list(
            [taxonomy["primary_category"]] + manual_categories + [fetched.get("metadata", {}).get("source_type", ""), fetched.get("metadata", {}).get("product_kind", "")]
        ),
        "subcategories": taxonomy["subcategories"],
        "knowledge_types": taxonomy["knowledge_types"],
        "usage_intents": taxonomy["usage_intents"],
        "tags": taxonomy["tags"],
        "documents_count": len(units),
        "document_ids": [item["id"] for item in units[:20]],
        "preview_chunks": [{"kind": item["metadata"]["chunk_kind"], "text": item["document"][:220]} for item in units[:8]],
    }


def search_knowledge(query: str, limit: int = 5, category: str | None = None) -> dict:
    where = {"primary_category": category} if category else None
    results = search_documents(query, n_results=limit, where=where)
    return {
        "query": query,
        "results": [
            {
                "id": item["id"],
                "document": item["document"],
                "metadata": item["metadata"],
                "distance": item.get("distance"),
            }
            for item in results
        ],
    }


def get_taxonomy_summary(category: str | None = None) -> dict:
    docs = get_documents(where={"primary_category": category}) if category else get_documents()
    primary_categories: list[str] = []
    subcategories: list[str] = []
    knowledge_types: list[str] = []
    usage_intents: list[str] = []
    tags: list[str] = []
    source_ids: set[str] = set()
    source_urls: set[str] = set()

    for item in docs:
        metadata = item.get("metadata", {})
        primary = str(metadata.get("primary_category") or "").strip()
        if primary:
            primary_categories.append(primary)
        subcategories.extend(_csv_to_list(metadata.get("subcategories")))
        knowledge_types.extend(_csv_to_list(metadata.get("knowledge_types")))
        usage_intents.extend(_csv_to_list(metadata.get("usage_intents")))
        tags.extend(_csv_to_list(metadata.get("tags")))
        source_id = str(metadata.get("source_id") or "").strip()
        source_url = str(metadata.get("source_url") or "").strip()
        if source_id:
            source_ids.add(source_id)
        if source_url:
            source_urls.add(source_url)

    canonical_primary = _canonicalize_labels(primary_categories, [])
    selected_category = category or (canonical_primary[0] if len(canonical_primary) == 1 else "")
    return {
        "primary_category": selected_category,
        "available_primary_categories": canonical_primary,
        "subcategories": _canonicalize_labels(subcategories, []),
        "knowledge_types": _canonicalize_labels(knowledge_types, []),
        "usage_intents": _canonicalize_labels(usage_intents, []),
        "tags": _canonicalize_labels(tags, [], limit=50),
        "source_count": len(source_ids),
        "document_count": len(docs),
        "source_urls": sorted(source_urls)[:100],
    }


def list_rag_sources(category: str | None = None, search: str | None = None, limit: int = 100) -> dict:
    docs = get_documents(where={"primary_category": category}) if category else get_documents()
    grouped: dict[str, dict] = {}
    search_lower = (search or "").strip().lower()

    for item in docs:
        metadata = item.get("metadata", {})
        source_id = str(metadata.get("source_id") or "").strip()
        if not source_id:
            continue
        source_url = str(metadata.get("source_url") or "").strip()
        title = str(metadata.get("title") or "").strip()
        if search_lower:
            haystack = " ".join(
                [
                    source_url,
                    title,
                    str(metadata.get("primary_category") or ""),
                    str(metadata.get("subcategories") or ""),
                    str(metadata.get("knowledge_types") or ""),
                    str(metadata.get("tags") or ""),
                ]
            ).lower()
            if search_lower not in haystack:
                continue
        if source_id not in grouped:
            grouped[source_id] = {
                "source_id": source_id,
                "source_url": source_url,
                "title": title,
                "primary_category": str(metadata.get("primary_category") or ""),
                "source_type": str(metadata.get("source_type") or ""),
                "product_kind": str(metadata.get("product_kind") or ""),
                "subcategories": [],
                "knowledge_types": [],
                "usage_intents": [],
                "tags": [],
                "document_count": 0,
                "last_ingested_at": str(metadata.get("ingested_at") or ""),
            }
        entry = grouped[source_id]
        entry["document_count"] += 1
        entry["subcategories"] = _canonicalize_labels(entry["subcategories"] + _csv_to_list(metadata.get("subcategories")), [])
        entry["knowledge_types"] = _canonicalize_labels(entry["knowledge_types"] + _csv_to_list(metadata.get("knowledge_types")), [])
        entry["usage_intents"] = _canonicalize_labels(entry["usage_intents"] + _csv_to_list(metadata.get("usage_intents")), [])
        entry["tags"] = _canonicalize_labels(entry["tags"] + _csv_to_list(metadata.get("tags")), [], limit=20)
        ingested_at = str(metadata.get("ingested_at") or "")
        if ingested_at and ingested_at > entry["last_ingested_at"]:
            entry["last_ingested_at"] = ingested_at

    sources = sorted(grouped.values(), key=lambda item: item["last_ingested_at"], reverse=True)
    return {
        "total": len(sources),
        "sources": sources[: max(1, min(limit, 200))],
    }


def get_source_documents(url: str) -> dict:
    source_url = _canonical_url(url)
    source_id = _source_id(source_url)
    docs = get_documents(where={"source_id": source_id})
    return {
        "source_id": source_id,
        "source_url": source_url,
        "documents_count": len(docs),
        "documents": docs,
    }


def delete_source_documents(url: str) -> dict:
    source_url = _canonical_url(url)
    source_id = _source_id(source_url)
    deleted_count = delete_documents(where={"source_id": source_id})
    return {
        "source_id": source_id,
        "source_url": source_url,
        "deleted_count": deleted_count,
    }


def _candidate_primary_categories() -> list[str]:
    taxonomy = _existing_taxonomy()
    return taxonomy["primary_categories"][:20]


def _heuristic_knowledge_plan(key_points: list[str], metadata: dict, extracted: dict | None = None) -> dict:
    extracted = extracted or {}
    title = str(metadata.get("title") or "").strip()
    lower = " ".join(
        [
            title,
            " ".join(str(item) for item in key_points[:4]),
            " ".join(str(item) for item in extracted.get("important_facts", [])[:6]),
            " ".join(str(item) for item in extracted.get("product_use_cases", [])[:4]),
        ]
    ).lower()
    primary_category = ""
    if "trà" in lower:
        primary_category = "trà"
    elif any(term in lower for term in ["áo", "váy", "quần", "thời trang", "linen"]):
        primary_category = "thời trang"
    queries = _normalize_list(
        [
            title,
            " ".join(key_points[:2]),
            " ".join(extracted.get("important_facts", [])[:2]),
            " ".join(extracted.get("product_use_cases", [])[:2]),
        ]
    )[:4]
    return {
        "primary_category": primary_category,
        "queries": [item for item in queries if item][:3],
        "desired_knowledge_types": [],
    }


def _build_candidate_pool(plan: dict, limit: int = 12) -> list[dict]:
    queries = _normalize_list(plan.get("queries") or [])[:4]
    primary_category = str(plan.get("primary_category") or "").strip()
    where = {"primary_category": primary_category} if primary_category else None
    candidates: list[dict] = []
    seen: set[str] = set()
    for query in queries:
        for item in search_documents(query, n_results=min(8, max(4, limit)), where=where):
            candidate_id = item["id"]
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            candidates.append(item)
            if len(candidates) >= limit:
                return candidates
    if not candidates and where is None and queries:
        for item in search_documents(queries[0], n_results=limit):
            candidate_id = item["id"]
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            candidates.append(item)
    return candidates[:limit]


def _fallback_select(candidates: list[dict]) -> list[dict]:
    selected: list[dict] = []
    for item in candidates[:4]:
        metadata = item.get("metadata", {})
        selected.append(
            {
                "fact": item.get("document", ""),
                "source": metadata.get("source_url") or metadata.get("title") or "",
                "source_url": metadata.get("source_url", ""),
                "knowledge_type": _csv_to_list(metadata.get("knowledge_types"))[0] if metadata.get("knowledge_types") else "",
                "subcategories": _csv_to_list(metadata.get("subcategories")),
                "usage_intent": _csv_to_list(metadata.get("usage_intents"))[0] if metadata.get("usage_intents") else "",
                "integration_hint": "Chỉ dùng nếu giúp người đọc hiểu rõ hơn và không làm bài gượng.",
            }
        )
    return selected


def select_knowledge_for_content(
    key_points: list[str],
    metadata: dict,
    extracted: dict | None = None,
    limit: int = 6,
) -> list[dict]:
    available_primary_categories = _candidate_primary_categories()
    if not available_primary_categories:
        return []
    extracted = extracted or {}
    fallback_plan = _heuristic_knowledge_plan(key_points, metadata, extracted)
    prompt = (
        f"Available primary categories: {available_primary_categories}\n"
        f"Metadata: {metadata}\n"
        f"Key points: {key_points[:5]}\n"
        f"Important facts: {extracted.get('important_facts', [])[:6]}\n"
        f"Product use cases: {extracted.get('product_use_cases', [])[:4]}\n"
        f"Buyer objections: {extracted.get('buyer_objections', [])[:4]}\n"
        f"FAQ items: {extracted.get('faq_items', [])[:3]}\n"
        "Tra ve JSON hop le voi cac truong: primary_category, queries, desired_knowledge_types.\n"
        "Chi chon primary_category neu that su phu hop; neu khong chac, de rong.\n"
    )
    plan = call_json("knowledge", RAG_SEARCH_PLAN_SYSTEM_PROMPT, prompt, fallback=fallback_plan, max_tokens=400)
    normalized_plan = {
        "primary_category": str(plan.get("primary_category") or fallback_plan["primary_category"]).strip(),
        "queries": _normalize_list(plan.get("queries") if isinstance(plan.get("queries"), list) else fallback_plan["queries"])[:4],
        "desired_knowledge_types": _normalize_list(
            plan.get("desired_knowledge_types") if isinstance(plan.get("desired_knowledge_types"), list) else fallback_plan["desired_knowledge_types"]
        )[:4],
    }
    if normalized_plan["primary_category"] and normalized_plan["primary_category"] not in available_primary_categories:
        matched = next(
            (item for item in available_primary_categories if _labels_equivalent(normalized_plan["primary_category"], item)),
            "",
        )
        normalized_plan["primary_category"] = matched

    candidates = _build_candidate_pool(normalized_plan, limit=max(8, limit * 2))
    if not candidates:
        return []
    candidate_summaries = [
        {
            "id": item["id"],
            "document": item["document"][:280],
            "metadata": {
                "title": item.get("metadata", {}).get("title", ""),
                "source_url": item.get("metadata", {}).get("source_url", ""),
                "primary_category": item.get("metadata", {}).get("primary_category", ""),
                "subcategories": _csv_to_list(item.get("metadata", {}).get("subcategories")),
                "knowledge_types": _csv_to_list(item.get("metadata", {}).get("knowledge_types")),
                "usage_intents": _csv_to_list(item.get("metadata", {}).get("usage_intents")),
                "chunk_kind": item.get("metadata", {}).get("chunk_kind", ""),
            },
        }
        for item in candidates
    ]
    fallback_selected = {"selected": [{"candidate_id": item["id"], "fact": item["document"], "integration_hint": ""} for item in candidates[: min(limit, 4)]]}
    selection_prompt = (
        f"Metadata hien tai: {metadata}\n"
        f"Key points: {key_points[:5]}\n"
        f"Important facts: {extracted.get('important_facts', [])[:6]}\n"
        f"Product use cases: {extracted.get('product_use_cases', [])[:4]}\n"
        f"Buyer objections: {extracted.get('buyer_objections', [])[:4]}\n"
        f"Search plan: {normalized_plan}\n"
        f"Candidates: {candidate_summaries}\n"
    )
    data = call_json("knowledge", RAG_SELECTION_SYSTEM_PROMPT, selection_prompt, fallback=fallback_selected, max_tokens=900)
    picked_ids = []
    for item in data.get("selected", []) if isinstance(data.get("selected"), list) else []:
        candidate_id = str(item.get("candidate_id") or "").strip()
        if candidate_id and candidate_id not in picked_ids:
            picked_ids.append(candidate_id)
        if len(picked_ids) >= limit:
            break

    if not picked_ids:
        return _fallback_select(candidates)[:limit]

    by_id = {item["id"]: item for item in candidates}
    selected_output: list[dict] = []
    seen_candidate_ids: set[str] = set()
    for selected in data.get("selected", []):
        candidate_id = str(selected.get("candidate_id") or "").strip()
        candidate = by_id.get(candidate_id)
        if not candidate or candidate_id in seen_candidate_ids:
            continue
        seen_candidate_ids.add(candidate_id)
        meta = candidate.get("metadata", {})
        selected_output.append(
            {
                "fact": str(selected.get("fact") or candidate.get("document") or "").strip(),
                "source": meta.get("title") or meta.get("source_url") or "",
                "source_url": meta.get("source_url", ""),
                "knowledge_type": (_csv_to_list(meta.get("knowledge_types")) or [""])[0],
                "subcategories": _csv_to_list(meta.get("subcategories")),
                "usage_intent": (_csv_to_list(meta.get("usage_intents")) or [""])[0],
                "integration_hint": str(selected.get("integration_hint") or "").strip(),
                "reason": str(selected.get("reason") or "").strip(),
            }
        )
        if len(selected_output) >= limit:
            break
    return selected_output or _fallback_select(candidates)[:limit]
