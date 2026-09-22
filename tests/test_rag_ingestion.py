"""Tests for the ingestion pipeline: chunking + upsert + idempotency + persistence."""
from __future__ import annotations

from pathlib import Path

from app.rag.chunking import chunk_document
from app.rag.documents import load_policy_documents
from app.rag.embeddings import DeterministicHashEmbedding
from app.rag.ingest import ingest_policy_directory
from app.rag.vector_store import PolicyVectorStore


def _expected_chunk_count(policy_dir: Path) -> int:
    return sum(len(chunk_document(d)) for d in load_policy_documents(policy_dir))


def test_ingest_populates_the_expected_chunk_count(tmp_path: Path, policy_dir: Path) -> None:
    provider = DeterministicHashEmbedding()
    store = PolicyVectorStore(path=tmp_path / "chroma", collection_name="ingest_test", embedding_provider=provider)

    summary = ingest_policy_directory(policy_dir, vector_store=store)

    expected_chunks = _expected_chunk_count(policy_dir)
    assert summary.chunk_count == expected_chunks
    assert summary.document_count == len(load_policy_documents(policy_dir))
    assert store.count() == expected_chunks


def test_ingest_is_idempotent(tmp_path: Path, policy_dir: Path) -> None:
    provider = DeterministicHashEmbedding()
    store = PolicyVectorStore(path=tmp_path / "chroma", collection_name="ingest_test", embedding_provider=provider)

    ingest_policy_directory(policy_dir, vector_store=store)
    first_count = store.count()

    ingest_policy_directory(policy_dir, vector_store=store)
    second_count = store.count()

    assert first_count == second_count == _expected_chunk_count(policy_dir)


def test_reingesting_replaces_content_for_a_changed_chunk(tmp_path: Path) -> None:
    from datetime import date

    from app.rag.chunking import DocumentChunk

    provider = DeterministicHashEmbedding()
    store = PolicyVectorStore(path=tmp_path / "chroma", collection_name="ingest_test", embedding_provider=provider)

    def make_chunk(text: str) -> DocumentChunk:
        return DocumentChunk(
            chunk_id="doc-x::chunk::0",
            document_id="doc-x",
            title="Doc X",
            section="Only Section",
            document_type="test_policy",
            department="ALL",
            audience="student",
            policy_version="v1",
            effective_from=date(2024, 1, 1),
            effective_to=None,
            visibility="public",
            chunk_index=0,
            source="data/policies/doc-x.md",
            text=text,
        )

    store.upsert_chunks([make_chunk("original text")])
    assert store.count() == 1

    store.upsert_chunks([make_chunk("updated text")])
    assert store.count() == 1  # same chunk_id -> replaced, not duplicated

    result = store.get(where={"document_id": "doc-x"})
    assert result["documents"] == ["updated text"]


def test_chroma_collection_persists_across_client_instances(tmp_path: Path, policy_dir: Path) -> None:
    chroma_path = tmp_path / "chroma"
    provider = DeterministicHashEmbedding()

    store_a = PolicyVectorStore(path=chroma_path, collection_name="persist_test", embedding_provider=provider)
    ingest_policy_directory(policy_dir, vector_store=store_a)
    count_after_ingest = store_a.count()

    # A brand-new PolicyVectorStore instance at the same path/collection name
    # must see the previously-persisted data.
    store_b = PolicyVectorStore(path=chroma_path, collection_name="persist_test", embedding_provider=provider)
    assert store_b.count() == count_after_ingest


def test_deterministic_embedding_is_reproducible() -> None:
    provider_a = DeterministicHashEmbedding()
    provider_b = DeterministicHashEmbedding()

    text = "Students must maintain a minimum of 75% attendance."
    assert provider_a.embed([text]) == provider_b.embed([text])


def test_deterministic_embedding_needs_no_network(monkeypatch) -> None:
    """Guard against accidentally routing the deterministic provider through I/O."""
    import socket

    def _blocked(*args, **kwargs):
        raise AssertionError("DeterministicHashEmbedding must not touch the network")

    monkeypatch.setattr(socket, "socket", _blocked)
    provider = DeterministicHashEmbedding()
    vectors = provider.embed(["a policy about attendance and exams"])
    assert len(vectors) == 1
    assert len(vectors[0]) == provider.dimension
