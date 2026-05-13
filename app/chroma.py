from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, List

from app.config import get_settings

try:
    import chromadb
except ImportError:  # pragma: no cover
    chromadb = None

try:
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover
    SentenceTransformer = None


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


def _safe_collection_name(name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")
    normalized = re.sub(r"_{2,}", "_", normalized)
    if len(normalized) < 3:
        normalized = f"rag_{normalized or 'kb'}"
    return normalized[:63]


def get_collection_name() -> str:
    settings = get_settings()
    if settings.rag_collection_name:
        return _safe_collection_name(settings.rag_collection_name)
    model_suffix = _safe_collection_name(settings.rag_embedding_model)
    return _safe_collection_name(f"knowledge_base_{model_suffix}")


def get_named_collection_name(prefix: str) -> str:
    settings = get_settings()
    model_suffix = _safe_collection_name(settings.rag_embedding_model)
    return _safe_collection_name(f"{prefix}_{model_suffix}")


@lru_cache(maxsize=4)
def get_embedding_function(model_name: str, max_tokens: int | None = None) -> Any:
    if SentenceTransformer is None:
        return None
    model = SentenceTransformer(model_name)
    if max_tokens and max_tokens > 0:
        model.max_seq_length = max_tokens

    class LocalSentenceTransformerEmbeddingFunction:
        def name(self) -> str:
            return "sentence_transformer"

        def _encode(self, texts: List[str]) -> List[List[float]]:
            embeddings = model.encode(
                list(texts),
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return embeddings.tolist()

        def __call__(self, input: List[str]) -> List[List[float]]:  # Chroma requires the parameter name `input`.
            return self._encode(input)

        def embed_documents(self, texts: List[str]) -> List[List[float]]:
            return self._encode(texts)

        def embed_query(self, text: str) -> List[float]:
            return self._encode([text])[0]

    return LocalSentenceTransformerEmbeddingFunction()


def get_collection(use_embedding: bool = True, fallback_on_error: bool = True, collection_name: str | None = None) -> Any:
    settings = get_settings()
    if chromadb is None:
        return _memory_db
    try:
        client = chromadb.PersistentClient(path=settings.chroma_path)
        name = _safe_collection_name(collection_name) if collection_name else get_collection_name()
        if not use_embedding:
            return client.get_collection(name)
        kwargs: Dict[str, Any] = {"metadata": {"embedding_model": settings.rag_embedding_model}}
        kwargs["embedding_function"] = get_embedding_function(settings.rag_embedding_model, settings.rag_embedding_max_tokens)
        return client.get_or_create_collection(name, **kwargs)
    except Exception:
        if not fallback_on_error:
            raise
        return _memory_db


def add_document(document: str, metadata: Dict[str, Any], doc_id: str, collection_name: str | None = None) -> None:
    collection = get_collection(use_embedding=True, fallback_on_error=False, collection_name=collection_name)
    if isinstance(collection, InMemoryKnowledgeBase):
        collection.add(document=document, metadata=metadata, doc_id=doc_id)
    else:
        collection.add(documents=[document], metadatas=[metadata], ids=[doc_id])


def add_documents(documents: List[Dict[str, Any]], collection_name: str | None = None) -> None:
    if not documents:
        return
    collection = get_collection(use_embedding=True, fallback_on_error=False, collection_name=collection_name)
    if isinstance(collection, InMemoryKnowledgeBase):
        for item in documents:
            collection.add(document=item["document"], metadata=item["metadata"], doc_id=item["id"])
        return
    collection.add(
        documents=[item["document"] for item in documents],
        metadatas=[item["metadata"] for item in documents],
        ids=[item["id"] for item in documents],
    )


def get_documents(where: Dict[str, Any] | None = None, collection_name: str | None = None) -> List[Dict[str, Any]]:
    collection = get_collection(use_embedding=False, collection_name=collection_name)
    if isinstance(collection, InMemoryKnowledgeBase):
        return collection.get(where=where)
    try:
        limit = max(int(collection.count()), 100)
    except Exception:
        limit = 10000
    result = collection.get(where=where, limit=limit)
    documents = result.get("documents", [])
    metadatas = result.get("metadatas", [])
    ids = result.get("ids", [])
    return [
        {"id": ids[idx], "document": documents[idx], "metadata": metadatas[idx]}
        for idx in range(min(len(documents), len(metadatas), len(ids)))
    ]


def delete_documents(where: Dict[str, Any] | None = None, ids: List[str] | None = None, collection_name: str | None = None) -> int:
    collection = get_collection(use_embedding=False, collection_name=collection_name)
    if isinstance(collection, InMemoryKnowledgeBase):
        return collection.delete(where=where, ids=ids)
    before = len(get_documents(where=where, collection_name=collection_name)) if where else (len(ids) if ids else 0)
    if ids:
        collection.delete(ids=ids)
    elif where:
        collection.delete(where=where)
    else:
        collection.delete()
    return before


def query_documents(query_text: str, n_results: int = 3, published_only: bool = False, collection_name: str | None = None) -> List[Dict[str, Any]]:
    collection = get_collection(use_embedding=True, fallback_on_error=False, collection_name=collection_name)
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


def search_documents(query_text: str, n_results: int = 5, where: Dict[str, Any] | None = None, collection_name: str | None = None) -> List[Dict[str, Any]]:
    collection = get_collection(use_embedding=True, fallback_on_error=False, collection_name=collection_name)
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
