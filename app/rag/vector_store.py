"""ChromaDB persistence for policy chunks.

Thin, typed wrapper around a single Chroma collection. All embedding is done
by the caller-supplied EmbeddingProvider and passed in explicitly (via
``embeddings=``/``query_embeddings=``) rather than registering a
Chroma-native embedding function -- this keeps the vector store agnostic to
which provider produced the vectors and makes provider-swapping trivial.

``upsert_chunks`` uses Chroma's ``upsert`` (keyed on the deterministic
``chunk_id``), so re-running ingestion on unchanged documents/chunk layout
never creates duplicate rows -- it just replaces the existing ones in place.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb

from app.rag.chunking import DocumentChunk
from app.rag.embeddings import EmbeddingProvider


def _chunk_metadata(chunk: DocumentChunk) -> Dict[str, Any]:
    """Flatten a DocumentChunk into Chroma-safe metadata (str/int/float/bool only)."""
    return {
        "document_id": chunk.document_id,
        "title": chunk.title,
        "section": chunk.section,
        "document_type": chunk.document_type,
        "department": chunk.department,
        "audience": chunk.audience,
        "policy_version": chunk.policy_version,
        "effective_from": chunk.effective_from.isoformat(),
        "effective_to": chunk.effective_to.isoformat() if chunk.effective_to else "",
        "visibility": chunk.visibility,
        "chunk_index": chunk.chunk_index,
        "source": chunk.source,
    }


def build_where(conditions: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a Chroma ``where`` filter, wrapping multiple conditions in ``$and``.

    Chroma 1.x rejects a plain multi-key dict for query() ("expected where to
    have exactly one operator"), so more than one condition must be an
    explicit ``$and`` of single-key dicts.
    """
    present = {k: v for k, v in conditions.items() if v is not None}
    if not present:
        return None
    if len(present) == 1:
        (key, value), = present.items()
        return {key: value}
    return {"$and": [{k: v} for k, v in present.items()]}


class PolicyVectorStore:
    """A single Chroma collection of policy chunks, with idempotent upserts."""

    def __init__(self, *, path: Path, collection_name: str, embedding_provider: EmbeddingProvider) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(path))
        self._embedding_provider = embedding_provider
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"embedding_provider": embedding_provider.name},
        )

    @property
    def embedding_provider(self) -> EmbeddingProvider:
        return self._embedding_provider

    def count(self) -> int:
        return self._collection.count()

    def upsert_chunks(self, chunks: List[DocumentChunk]) -> int:
        """Idempotently write ``chunks``; re-upserting the same chunk_id replaces it."""
        if not chunks:
            return 0
        texts = [c.text for c in chunks]
        embeddings = self._embedding_provider.embed(texts)
        self._collection.upsert(
            ids=[c.chunk_id for c in chunks],
            embeddings=embeddings,
            documents=texts,
            metadatas=[_chunk_metadata(c) for c in chunks],
        )
        return len(chunks)

    def query(
        self,
        *,
        query_embedding: List[float],
        top_k: int,
        where: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

    def get(self, *, where: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._collection.get(where=where, include=["documents", "metadatas"])
