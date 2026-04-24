from __future__ import annotations

import argparse
import json

from app.rag import delete_source_documents, get_source_documents, ingest_url, search_knowledge


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAG knowledge CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Ingest a source URL into Chroma")
    ingest.add_argument("url", help="Source URL to ingest")
    ingest.add_argument("--category", action="append", default=[], dest="categories", help="Manual category, repeatable")
    ingest.add_argument("--tag", action="append", default=[], dest="tags", help="Manual tag, repeatable")
    ingest.add_argument("--note", default=None, help="Optional note for the source")
    ingest.add_argument("--no-force", action="store_true", help="Skip reingest if source already exists")

    search = subparsers.add_parser("search", help="Search knowledge documents")
    search.add_argument("query", help="Search query")
    search.add_argument("--limit", type=int, default=5, help="Number of results")
    search.add_argument("--category", default=None, help="Primary category filter")

    source = subparsers.add_parser("source", help="Show stored documents for a source URL")
    source.add_argument("url", help="Source URL")

    delete_source = subparsers.add_parser("delete-source", help="Delete stored documents for a source URL")
    delete_source.add_argument("url", help="Source URL")

    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()

    if args.command == "ingest":
        result = ingest_url(
            args.url,
            manual_categories=args.categories,
            manual_tags=args.tags,
            note=args.note,
            force_reingest=not args.no_force,
        )
    elif args.command == "search":
        result = search_knowledge(args.query, limit=max(1, min(args.limit, 20)), category=args.category)
    elif args.command == "source":
        result = get_source_documents(args.url)
    elif args.command == "delete-source":
        result = delete_source_documents(args.url)
    else:
        raise RuntimeError(f"Unsupported command: {args.command}")

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
