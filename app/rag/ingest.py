"""Reusable ingestion pipeline: parse policy documents, chunk, upsert.

Kept separate from scripts/ingest_policies.py so tests can ingest a small
document set into a temp vector store without shelling out to a script.
Idempotency comes from PolicyVectorStore.upsert_chunks (Chroma upsert keyed
on the deterministic chunk_id) -- re-running this on an unchanged corpus
replaces existing rows in place rather than duplicating them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from app.rag.chunking import chunk_document
from app.rag.documents import PolicyDocument, load_policy_documents
from app.rag.vector_store import PolicyVectorStore


@dataclass(frozen=True)
class IngestSummary:
    document_count: int
    chunk_count: int
    document_ids: List[str] = field(default_factory=list)


def ingest_documents(documents: List[PolicyDocument], *, vector_store: PolicyVectorStore) -> IngestSummary:
    total_chunks = 0
    for document in documents:
        chunks = chunk_document(document)
        vector_store.upsert_chunks(chunks)
        total_chunks += len(chunks)
    return IngestSummary(
        document_count=len(documents),
        chunk_count=total_chunks,
        document_ids=[d.document_id for d in documents],
    )


def ingest_policy_directory(policy_dir: Path, *, vector_store: PolicyVectorStore) -> IngestSummary:
    documents = load_policy_documents(policy_dir)
    return ingest_documents(documents, vector_store=vector_store)
