"""The Knowledge Service: deterministic retrieval infrastructure for Phase 3.

This is the KnowledgeService called out in the Phase 3 plan -- the
deterministic retrieval half of the future Knowledge/RAG Agent (CLAUDE.md
component 6). It does not plan missions, generate final answers, make policy
decisions, or invent facts; it only converts a query (+ filters) into ranked
app.schemas.evidence.Evidence citations traceable to real source chunks.

query -> retrieve -> filter -> rank -> return structured Evidence.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import List, Optional

from app.rag.retriever import PolicyRetriever, RetrievalQuery, RetrievalResult
from app.schemas.evidence import Evidence


def _to_utc_datetime(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _to_evidence(result: RetrievalResult) -> Evidence:
    return Evidence(
        evidence_id=f"ev-{result.chunk_id}",
        document_id=result.document_id,
        title=result.title,
        source=result.source,
        snippet=result.text,
        section=result.section,
        policy_version=result.policy_version,
        effective_from=_to_utc_datetime(result.effective_from),
        effective_to=_to_utc_datetime(result.effective_to) if result.effective_to else None,
        relevance_score=max(0.0, min(1.0, result.score)),
    )


class KnowledgeService:
    """Deterministic accessor over the policy vector store. No LLM calls."""

    def __init__(self, *, retriever: PolicyRetriever) -> None:
        self._retriever = retriever

    def search(
        self,
        query: str,
        *,
        as_of: Optional[date] = None,
        document_type: Optional[str] = None,
        department: Optional[str] = None,
        audience: Optional[str] = None,
        visibility: Optional[str] = None,
        document_id: Optional[str] = None,
        include_historical: bool = False,
        top_k: int = 5,
    ) -> List[Evidence]:
        """Ranked evidence for a free-text query, optionally filtered.

        Returns [] if nothing clears the retriever's relevance threshold --
        this is the explicit "no evidence found" state; it never fabricates
        or pads results with weak matches.
        """
        if not query or not query.strip():
            return []
        retrieval_query = RetrievalQuery(
            query=query,
            as_of=as_of,
            include_historical=include_historical,
            document_type=document_type,
            department=department,
            audience=audience,
            visibility=visibility,
            document_id=document_id,
            top_k=top_k,
        )
        results = self._retriever.retrieve(retrieval_query)
        return [_to_evidence(r) for r in results]

    def get_active_policy(
        self,
        document_type: str,
        *,
        as_of: Optional[date] = None,
        department: Optional[str] = None,
        audience: Optional[str] = None,
        visibility: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> List[Evidence]:
        """Every chunk of the policy version active for ``document_type`` as of ``as_of``.

        Pure metadata + date-window filtering (no semantic ranking needed --
        document_type plus the active-version window already pins the
        result set), returned in document reading order.
        """
        results = self._retriever.get_active_chunks(
            document_type=document_type,
            as_of=as_of or date.today(),
            department=department,
            audience=audience,
            visibility=visibility,
        )
        if top_k is not None:
            results = results[:top_k]
        return [_to_evidence(r) for r in results]

    def get_document_evidence(self, document_id: str, *, top_k: Optional[int] = None) -> List[Evidence]:
        """Every chunk of a specific document_id (any version), in reading order."""
        results = self._retriever.get_document_chunks(document_id)
        if top_k is not None:
            results = results[:top_k]
        return [_to_evidence(r) for r in results]
