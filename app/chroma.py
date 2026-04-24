from __future__ import annotations

from typing import Any, Dict, List

from app.config import get_settings

try:
    import chromadb
except ImportError:  # pragma: no cover
    chromadb = None


class InMemoryKnowledgeBase:
    def __init__(self) -> None:
        self.documents: List[Dict[str, Any]] = []

    def add(self, document: str, metadata: Dict[str, Any], doc_id: str) -> None:
        self.documents = [item for item in self.documents if item["id"] != doc_id]
        self.documents.append({"id": doc_id, "document": document, "metadata": metadata})

    def get(self, where: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
        if not where:
            return list(self.documents)
        results: List[Dict[str, Any]] = []
        for item in self.documents:
            metadata = item["metadata"]
            if all(metadata.get(key) == value for key, value in where.items()):
                results.append(item)
        return results

    def delete(self, where: Dict[str, Any] | None = None, ids: List[str] | None = None) -> int:
        before = len(self.documents)
        if ids:
            id_set = set(ids)
            self.documents = [item for item in self.documents if item["id"] not in id_set]
            return before - len(self.documents)
        if where:
            self.documents = [
                item for item in self.documents
                if not all(item["metadata"].get(key) == value for key, value in where.items())
            ]
            return before - len(self.documents)
        self.documents = []
        return before

    def query(self, query_text: str, n_results: int = 3, published_only: bool = False) -> List[Dict[str, Any]]:
        scored = []
        q_terms = set(query_text.lower().split())
        for item in self.documents:
            metadata = item["metadata"]
            if published_only and metadata.get("status") != "published":
                continue
            text = " ".join(
                str(part or "")
                for part in [
                    item["document"],
                    metadata.get("title", ""),
                    metadata.get("keywords", ""),
                    metadata.get("primary_category", ""),
                    metadata.get("categories", ""),
                    metadata.get("subcategories", ""),
                    metadata.get("knowledge_types", ""),
                    metadata.get("usage_intents", ""),
                    metadata.get("tags", ""),
                    metadata.get("chunk_kind", ""),
                ]
            ).lower()
            score = sum(1 for term in q_terms if term in text)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:n_results]]


_memory_db = InMemoryKnowledgeBase()


def get_collection() -> Any:
    settings = get_settings()
    if chromadb is None:
        return _memory_db
    try:
        client = chromadb.PersistentClient(path=settings.chroma_path)
        return client.get_or_create_collection("knowledge_base")
    except Exception:
        return _memory_db


def add_document(document: str, metadata: Dict[str, Any], doc_id: str) -> None:
    collection = get_collection()
    if isinstance(collection, InMemoryKnowledgeBase):
        collection.add(document=document, metadata=metadata, doc_id=doc_id)
    else:
        collection.add(documents=[document], metadatas=[metadata], ids=[doc_id])


def get_documents(where: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    collection = get_collection()
    if isinstance(collection, InMemoryKnowledgeBase):
        return collection.get(where=where)
    result = collection.get(where=where)
    documents = result.get("documents", [])
    metadatas = result.get("metadatas", [])
    ids = result.get("ids", [])
    return [
        {"id": ids[idx], "document": documents[idx], "metadata": metadatas[idx]}
        for idx in range(min(len(documents), len(metadatas), len(ids)))
    ]


def delete_documents(where: Dict[str, Any] | None = None, ids: List[str] | None = None) -> int:
    collection = get_collection()
    if isinstance(collection, InMemoryKnowledgeBase):
        return collection.delete(where=where, ids=ids)
    before = len(get_documents(where=where)) if where else (len(ids) if ids else 0)
    if ids:
        collection.delete(ids=ids)
    elif where:
        collection.delete(where=where)
    else:
        collection.delete()
    return before


def query_documents(query_text: str, n_results: int = 3, published_only: bool = False) -> List[Dict[str, Any]]:
    collection = get_collection()
    if isinstance(collection, InMemoryKnowledgeBase):
        return collection.query(query_text=query_text, n_results=n_results, published_only=published_only)

    where = {"status": "published"} if published_only else None
    result = collection.query(query_texts=[query_text], n_results=n_results, where=where)
    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    ids = result.get("ids", [[]])[0]
    return [
        {"id": ids[idx], "document": documents[idx], "metadata": metadatas[idx]}
        for idx in range(min(len(documents), len(metadatas), len(ids)))
    ]


def search_documents(query_text: str, n_results: int = 5, where: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    collection = get_collection()
    if isinstance(collection, InMemoryKnowledgeBase):
        candidates = collection.query(query_text=query_text, n_results=max(20, n_results), published_only=False)
        if where:
            candidates = [
                item for item in candidates
                if all(item["metadata"].get(key) == value for key, value in where.items())
            ]
        return candidates[:n_results]
    result = collection.query(query_texts=[query_text], n_results=n_results, where=where)
    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    ids = result.get("ids", [[]])[0]
    distances = result.get("distances", [[]])[0] if result.get("distances") else []
    output = []
    for idx in range(min(len(documents), len(metadatas), len(ids))):
        item = {"id": ids[idx], "document": documents[idx], "metadata": metadatas[idx]}
        if idx < len(distances):
            item["distance"] = distances[idx]
        output.append(item)
    return output
