from __future__ import annotations

from app.rag import select_knowledge_for_content


def run(key_points: list[str], metadata: dict | None = None, extracted: dict | None = None) -> list[dict]:
    return select_knowledge_for_content(
        key_points=key_points,
        metadata=metadata or {},
        extracted=extracted or {},
        limit=6,
    )
