from __future__ import annotations

import argparse
from typing import Any

import chromadb

from app.chroma import get_collection, get_collection_name
from app.config import get_settings


def _batched(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Chroma RAG documents to the configured embedding collection.")
    parser.add_argument("--source", default="knowledge_base", help="Source Chroma collection name.")
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()

    settings = get_settings()
    target_name = get_collection_name()
    if args.source == target_name:
        raise SystemExit(f"Source and target are both {target_name}; nothing to migrate.")

    client = chromadb.PersistentClient(path=settings.chroma_path)
    try:
        source = client.get_collection(args.source)
    except Exception as error:
        raise SystemExit(f"Source collection not found: {args.source} ({error})") from error

    target = get_collection()
    result = source.get(include=["documents", "metadatas"])
    ids = result.get("ids", [])
    documents = result.get("documents", [])
    metadatas = result.get("metadatas", [])
    total = min(len(ids), len(documents), len(metadatas))
    if total == 0:
        print(f"No documents found in source collection: {args.source}")
        return

    migrated = 0
    for batch_ids in _batched(ids[:total], max(1, args.batch_size)):
        start = migrated
        end = start + len(batch_ids)
        target.upsert(
            ids=batch_ids,
            documents=documents[start:end],
            metadatas=metadatas[start:end],
        )
        migrated = end
        print(f"Migrated {migrated}/{total} documents -> {target_name}")

    print(
        "Done. "
        f"source={args.source}, target={target_name}, "
        f"embedding_model={settings.rag_embedding_model}, documents={migrated}"
    )


if __name__ == "__main__":
    main()
