"""Embeddings, tenant-isolated vector store, and the retrieval engine.

Adapted from Shri AI's retrieval/{embeddings,store,schema,engine}.py. The
single most important thing carried over unchanged: the vector store fails
CLOSED if a tenant_id filter is missing — a query can never accidentally
search across all tenants' data. This is the actual security-critical
piece of the whole RAG pipeline; everything else here is generic plumbing.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import chromadb
from openai import OpenAI
from pydantic import BaseModel, Field

COLLECTION_NAME = "business_ai_segments"


class UnboundTenantError(ValueError):
    """Raised when a vector search or write is attempted without a tenant_id."""


# ==============================================================================
# Embeddings
# ==============================================================================


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    @abstractmethod
    def dimension(self) -> int: ...

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]:
        return self.embed_texts([query])[0]


class OpenAIEmbeddingProvider(EmbeddingProvider):
    MODEL_DIMENSIONS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}

    def __init__(self, *, model_name: str, api_key: str) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for OpenAIEmbeddingProvider.")
        if model_name not in self.MODEL_DIMENSIONS:
            raise ValueError(f"Unsupported embedding model: {model_name}")
        self._model_name = model_name
        self._client = OpenAI(api_key=api_key)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self.MODEL_DIMENSIONS[self._model_name]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self._client.embeddings.create(model=self._model_name, input=texts)
                return [item.embedding for item in response.data]
            except Exception as exc:  # noqa: BLE001 - retry transient API errors
                last_error = exc
                if attempt < 3:
                    time.sleep(2**attempt)
        raise RuntimeError(f"Embedding request failed after 3 attempts") from last_error


class HashEmbeddingProvider(EmbeddingProvider):
    """Deterministic local embeddings for offline tests only — not semantically
    meaningful, never use in production.

    Bag-of-hashed-words (each word hashed to its own unit vector, then
    averaged) rather than hashing the whole string: this gives texts that
    share vocabulary a genuinely higher cosine similarity than unrelated
    texts, so tests that exercise the real retrieval + evidence-gate
    threshold (not just plumbing) get a meaningful signal instead of
    effectively-random noise.
    """

    DIMENSION = 128

    @property
    def model_name(self) -> str:
        return "hash-embedding-v1"

    @property
    def dimension(self) -> int:
        return self.DIMENSION

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _word_vector(self, word: str) -> list[float]:
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        values: list[float] = []
        while len(values) < self.DIMENSION:
            for byte in digest:
                values.append((byte / 255.0) * 2.0 - 1.0)
                if len(values) >= self.DIMENSION:
                    break
            digest = hashlib.sha256(digest).digest()
        return values

    def _embed_one(self, text: str) -> list[float]:
        words = re.findall(r"[a-z0-9]+", text.lower())
        if not words:
            words = ["__empty__"]
        totals = [0.0] * self.DIMENSION
        for word in words:
            vec = self._word_vector(word)
            for i, v in enumerate(vec):
                totals[i] += v
        norm = math.sqrt(sum(v * v for v in totals)) or 1.0
        return [v / norm for v in totals]


# ==============================================================================
# Evidence schema
# ==============================================================================


class Citation(BaseModel):
    label: str  # e.g. "Your FAQ" or "yourbusiness.com/pricing"
    source_url: str | None = None


class EvidenceItem(BaseModel):
    segment_id: str
    source_id: str
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    citation: Citation
    tenant_id: str


class EvidencePack(BaseModel):
    query: str
    tenant_id: str
    items: list[EvidenceItem] = Field(default_factory=list)
    top_k: int = 5

    @property
    def pack_confidence(self) -> float:
        if not self.items:
            return 0.0
        return max(item.confidence for item in self.items)


# ==============================================================================
# Tenant-isolated vector store (ChromaDB)
# ==============================================================================


class VectorStore:
    def __init__(self, store_root: Path | str) -> None:
        self.store_root = Path(store_root)
        self.store_root.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.store_root))
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )

    def upsert(
        self,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        if not ids:
            return
        for m in metadatas:
            if not m.get("tenant_id"):
                raise UnboundTenantError("Cannot index a segment without a tenant_id.")
        self._collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents)

    def search(self, query_embedding: list[float], *, tenant_id: str, top_k: int = 5) -> list[dict[str, Any]]:
        if not tenant_id:
            raise UnboundTenantError("Vector search failed closed: tenant_id is required.")
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where={"tenant_id": tenant_id},
            include=["metadatas", "distances", "documents"],
        )
        hits: list[dict[str, Any]] = []
        ids = results.get("ids", [[]])[0]
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        documents = results.get("documents", [[]])[0]
        for seg_id, distance, metadata, document in zip(ids, distances, metadatas, documents):
            hits.append(
                {
                    "segment_id": seg_id,
                    "confidence": max(0.0, min(1.0, 1.0 - distance)),
                    "metadata": metadata or {},
                    "text": document or "",
                }
            )
        return hits

    def count_for_tenant(self, tenant_id: str) -> int:
        if not tenant_id:
            raise UnboundTenantError("tenant_id is required.")
        result = self._collection.get(where={"tenant_id": tenant_id}, include=[])
        return len(result.get("ids", []))

    def delete_source(self, *, tenant_id: str, source_id: str) -> None:
        if not tenant_id:
            raise UnboundTenantError("tenant_id is required.")
        self._collection.delete(where={"$and": [{"tenant_id": tenant_id}, {"source_id": source_id}]})

    def delete_tenant(self, tenant_id: str) -> None:
        if not tenant_id:
            raise UnboundTenantError("tenant_id is required.")
        self._collection.delete(where={"tenant_id": tenant_id})


# ==============================================================================
# Retrieval engine
# ==============================================================================


class RetrievalEngine:
    def __init__(self, store: VectorStore, embeddings: EmbeddingProvider) -> None:
        self.store = store
        self.embeddings = embeddings

    def retrieve(self, query: str, *, tenant_id: str, top_k: int = 5) -> EvidencePack:
        query_embedding = self.embeddings.embed_query(query)
        hits = self.store.search(query_embedding, tenant_id=tenant_id, top_k=top_k)
        items = [
            EvidenceItem(
                segment_id=hit["segment_id"],
                source_id=hit["metadata"].get("source_id", "unknown"),
                text=hit["text"],
                confidence=hit["confidence"],
                citation=Citation(
                    label=hit["metadata"].get("source_label", "your knowledge base"),
                    source_url=hit["metadata"].get("source_url"),
                ),
                tenant_id=hit["metadata"].get("tenant_id", tenant_id),
            )
            for hit in hits
        ]
        return EvidencePack(query=query, tenant_id=tenant_id, items=items, top_k=top_k)
