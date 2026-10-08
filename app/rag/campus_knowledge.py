"""CAMPUS AI institutional knowledge: organization-scoped documents in the existing Chroma store.

The demo institution documents (``data/campus_ai_knowledge/``) are parsed and chunked by the existing RAG
pipeline (``app.rag.documents`` / ``app.rag.chunking``) and written to their own collection of the same Chroma
store, so the policy corpus that the legacy specialists and the RAG evaluation use is left exactly as it is.

Every chunk carries ``organization_id`` (plus ``source_id`` and ``scope``) in its metadata, and every search is
filtered by the caller's organization: a document of one institution can never be retrieved for another. A
search returns at most ``MAX_CHUNKS`` short chunks -- never a whole document, never the whole corpus.

Deterministic: no LLM is involved in indexing or retrieval.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from app.rag.chunking import chunk_document
from app.rag.config import BASE_DIR, get_rag_config
from app.rag.documents import _parse_front_matter, parse_policy_document
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.rag.retriever import PolicyRetriever, RetrievalQuery
from app.rag.vector_store import PolicyVectorStore

CAMPUS_KNOWLEDGE_DIR = BASE_DIR / "data" / "campus_ai_knowledge"
SOURCE_PREFIX = "data/campus_ai_knowledge"
COLLECTION_ENV = "CAMPUSNEXUS_CAMPUS_KNOWLEDGE_COLLECTION"
DEFAULT_COLLECTION = "campus_ai_knowledge"
MAX_CHUNKS = 4
DEFAULT_CHUNKS = 3
MAX_CHUNK_TEXT_CHARS = 700


class KnowledgeChunk(BaseModel):
    """One retrieved, citable chunk -- the only form institutional knowledge reaches an LLM in."""

    source_id: str
    document_id: str
    title: str
    section: str
    document_type: str
    text: str = Field(max_length=MAX_CHUNK_TEXT_CHARS)
    score: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class KnowledgeIndexSummary:
    organization_id: int
    document_count: int
    chunk_count: int
    skipped_documents: int


def _front_matter(path: Path) -> Dict[str, Any]:
    metadata, _ = _parse_front_matter(path.read_text(encoding="utf-8"), path=path)
    return metadata


def index_campus_knowledge(directory: Path, *, vector_store: PolicyVectorStore, organization_id: int,
                           organization_slug: str) -> KnowledgeIndexSummary:
    """Index every document of ``directory`` that belongs to ``organization_slug`` (front matter ``organization``).

    Chunk ids are prefixed with the organization, so re-indexing replaces rows in place (idempotent) and two
    organizations' chunks of a same-named document never collide."""
    documents = chunks_total = skipped = 0
    for path in sorted(Path(directory).glob("*.md")):
        extras = _front_matter(path)
        if str(extras.get("organization") or "") != organization_slug:
            skipped += 1
            continue
        document = parse_policy_document(path, source_prefix=SOURCE_PREFIX)
        chunks = [c.model_copy(update={"chunk_id": f"org{organization_id}::{c.chunk_id}"}) for c in chunk_document(document)]
        vector_store.upsert_chunks(chunks, extra_metadata={
            "organization_id": organization_id,
            "source_id": str(extras.get("source_id") or document.document_id),
            "scope": str(extras.get("scope") or "institution"),
        })
        documents += 1
        chunks_total += len(chunks)
    return KnowledgeIndexSummary(organization_id, documents, chunks_total, skipped)


class CampusKnowledge:
    """Organization-scoped retrieval of at most ``MAX_CHUNKS`` relevant chunks."""

    def __init__(self, retriever: PolicyRetriever) -> None:
        self._retriever = retriever

    def indexed_chunk_count(self) -> int:
        return self._retriever.indexed_chunk_count()

    def search(self, query: str, *, organization_id: Optional[int], document_types: Optional[Sequence[str]] = None,
               top_k: int = DEFAULT_CHUNKS) -> List[KnowledgeChunk]:
        """Top chunks for ``query`` inside ``organization_id`` only; [] when nothing relevant (or no organization)."""
        if organization_id is None or not (query or "").strip():
            return []
        results = self._retriever.retrieve(RetrievalQuery(
            query=query[:500], organization_id=organization_id, top_k=max(1, min(top_k, MAX_CHUNKS)),
            document_types=list(document_types) if document_types else None))
        return [KnowledgeChunk(
            source_id=r.source_id or r.document_id, document_id=r.document_id, title=r.title, section=r.section,
            document_type=r.document_type, text=r.text[:MAX_CHUNK_TEXT_CHARS], score=r.score,
        ) for r in results if r.organization_id == organization_id]


def open_campus_store(*, chroma_path: Optional[Path] = None, embedding_provider: Optional[EmbeddingProvider] = None,
                      collection_name: Optional[str] = None) -> PolicyVectorStore:
    config = get_rag_config(chroma_path=chroma_path)
    provider = embedding_provider or get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    name = collection_name or os.environ.get(COLLECTION_ENV) or DEFAULT_COLLECTION
    return PolicyVectorStore(path=config.chroma_path, collection_name=name, embedding_provider=provider)


def build_campus_knowledge(store: PolicyVectorStore) -> CampusKnowledge:
    return CampusKnowledge(PolicyRetriever(vector_store=store, embedding_provider=store.embedding_provider))
