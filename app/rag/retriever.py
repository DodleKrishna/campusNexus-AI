"""Deterministic hybrid retrieval over the policy vector store.

query -> embed -> Chroma semantic search (metadata pre-filtered) -> active
policy date filtering -> keyword rescoring -> ranked, score-thresholded
top-k. No LLM involved anywhere in this module.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.rag.embeddings import EmbeddingProvider, tokenize
from app.rag.vector_store import PolicyVectorStore, build_where
from app.schemas.common import NonBlankStr

DEFAULT_MIN_SCORE = 0.37
DEFAULT_OVERFETCH_MULTIPLIER = 6
DEFAULT_SEMANTIC_WEIGHT = 0.7
DEFAULT_KEYWORD_WEIGHT = 0.3


class RetrievalQuery(BaseModel):
    """A single retrieval request against the policy vector store."""

    query: NonBlankStr
    as_of: Optional[date] = None
    include_historical: bool = False
    document_type: Optional[str] = None
    department: Optional[str] = None
    audience: Optional[str] = None
    visibility: Optional[str] = None
    document_id: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=50)


class RetrievalResult(BaseModel):
    """One ranked, fully-cited retrieval hit -- everything Evidence needs."""

    chunk_id: NonBlankStr
    document_id: NonBlankStr
    title: NonBlankStr
    section: NonBlankStr
    document_type: NonBlankStr
    department: NonBlankStr
    audience: NonBlankStr
    policy_version: NonBlankStr
    effective_from: date
    effective_to: Optional[date] = None
    visibility: NonBlankStr
    source: NonBlankStr
    text: NonBlankStr
    score: float = Field(ge=0.0, le=1.0)


def _parse_optional_date(value: str) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def _row_from_metadata(chunk_id: str, text: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "text": text,
        "document_id": metadata["document_id"],
        "title": metadata["title"],
        "section": metadata["section"],
        "document_type": metadata["document_type"],
        "department": metadata["department"],
        "audience": metadata["audience"],
        "policy_version": metadata["policy_version"],
        "effective_from": date.fromisoformat(metadata["effective_from"]),
        "effective_to": _parse_optional_date(metadata.get("effective_to", "")),
        "visibility": metadata["visibility"],
        "source": metadata["source"],
        "chunk_index": metadata["chunk_index"],
    }


def _passes_active_filter(
    row: Dict[str, Any],
    *,
    as_of: Optional[date],
    include_historical: bool,
    document_id: Optional[str],
) -> bool:
    """Active-policy date filtering (CLAUDE.md/spec section 10).

    A document_id-scoped lookup always bypasses the filter (an explicit
    request for a specific version, including a superseded one); so does
    include_historical=True. Otherwise, when as_of is given, only chunks
    whose [effective_from, effective_to] window covers as_of pass.
    """
    if document_id is not None or include_historical or as_of is None:
        return True
    if row["effective_from"] > as_of:
        return False
    if row["effective_to"] is not None and row["effective_to"] < as_of:
        return False
    return True


def _keyword_score(query_tokens: set, text: str) -> float:
    if not query_tokens:
        return 0.0
    text_tokens = set(tokenize(text))
    overlap = query_tokens & text_tokens
    return len(overlap) / len(query_tokens)


def _to_result(score: float, row: Dict[str, Any]) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        title=row["title"],
        section=row["section"],
        document_type=row["document_type"],
        department=row["department"],
        audience=row["audience"],
        policy_version=row["policy_version"],
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        visibility=row["visibility"],
        source=row["source"],
        text=row["text"],
        score=max(0.0, min(1.0, score)),
    )


class PolicyRetriever:
    """Ranks vector-store hits with a semantic + keyword hybrid score."""

    def __init__(
        self,
        *,
        vector_store: PolicyVectorStore,
        embedding_provider: EmbeddingProvider,
        min_score: float = DEFAULT_MIN_SCORE,
        overfetch_multiplier: int = DEFAULT_OVERFETCH_MULTIPLIER,
        semantic_weight: float = DEFAULT_SEMANTIC_WEIGHT,
        keyword_weight: float = DEFAULT_KEYWORD_WEIGHT,
    ) -> None:
        self._vector_store = vector_store
        self._embedding_provider = embedding_provider
        self._min_score = min_score
        self._overfetch_multiplier = overfetch_multiplier
        self._semantic_weight = semantic_weight
        self._keyword_weight = keyword_weight

    def _where(self, query: RetrievalQuery) -> Optional[Dict[str, Any]]:
        return build_where(
            {
                "document_type": query.document_type,
                "department": query.department,
                "audience": query.audience,
                "visibility": query.visibility,
                "document_id": query.document_id,
            }
        )

    def retrieve(self, query: RetrievalQuery) -> List[RetrievalResult]:
        where = self._where(query)
        query_embedding = self._embedding_provider.embed([query.query])[0]
        overfetch = min(max(query.top_k * self._overfetch_multiplier, 20), 500)

        raw = self._vector_store.query(query_embedding=query_embedding, top_k=overfetch, where=where)
        ids = raw.get("ids") or [[]]
        documents = raw.get("documents") or [[]]
        metadatas = raw.get("metadatas") or [[]]
        distances = raw.get("distances") or [[]]
        if not ids or not ids[0]:
            return []

        query_tokens = set(tokenize(query.query))
        scored: List[tuple] = []
        for chunk_id, text, metadata, distance in zip(ids[0], documents[0], metadatas[0], distances[0]):
            row = _row_from_metadata(chunk_id, text, metadata)
            if not _passes_active_filter(
                row, as_of=query.as_of, include_historical=query.include_historical, document_id=query.document_id
            ):
                continue
            keyword_score = _keyword_score(query_tokens, text)
            if query_tokens and keyword_score == 0.0:
                # No shared vocabulary at all with the query -- treat as
                # unrelated rather than trusting a noisy semantic-only score.
                # This is what makes an off-topic query return no evidence
                # instead of padding out the top-k with weak matches.
                continue
            semantic_score = 1.0 / (1.0 + max(distance, 0.0))
            score = self._semantic_weight * semantic_score + self._keyword_weight * keyword_score
            if score < self._min_score:
                continue
            scored.append((score, row))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [_to_result(score, row) for score, row in scored[: query.top_k]]

    def get_document_chunks(self, document_id: str) -> List[RetrievalResult]:
        """All chunks of a specific document_id, in chunk order, unranked (score=1.0)."""
        raw = self._vector_store.get(where=build_where({"document_id": document_id}))
        rows = [
            _row_from_metadata(chunk_id, text, metadata)
            for chunk_id, text, metadata in zip(raw.get("ids", []), raw.get("documents", []), raw.get("metadatas", []))
        ]
        rows.sort(key=lambda r: r["chunk_index"])
        return [_to_result(1.0, row) for row in rows]

    def get_active_chunks(
        self,
        *,
        document_type: str,
        as_of: Optional[date] = None,
        department: Optional[str] = None,
        audience: Optional[str] = None,
        visibility: Optional[str] = None,
    ) -> List[RetrievalResult]:
        """All chunks matching document_type (+ optional filters) active as of ``as_of``.

        No embedding/ranking involved -- a pure metadata + date-window filter,
        sorted into a coherent reading order (document_id, then chunk order).
        """
        where = build_where(
            {"document_type": document_type, "department": department, "audience": audience, "visibility": visibility}
        )
        raw = self._vector_store.get(where=where)
        rows = [
            _row_from_metadata(chunk_id, text, metadata)
            for chunk_id, text, metadata in zip(raw.get("ids", []), raw.get("documents", []), raw.get("metadatas", []))
        ]
        rows = [row for row in rows if _passes_active_filter(row, as_of=as_of, include_historical=False, document_id=None)]
        rows.sort(key=lambda r: (r["document_id"], r["chunk_index"]))
        return [_to_result(1.0, row) for row in rows]
