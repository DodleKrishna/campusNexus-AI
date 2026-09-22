"""Tests for section-aware chunking of PolicyDocument -> DocumentChunk."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.rag.chunking import chunk_document
from app.rag.documents import DocumentSection, PolicyDocument, load_policy_documents


def _make_document(sections: list[DocumentSection], **overrides) -> PolicyDocument:
    defaults = dict(
        document_id="doc-1",
        title="Test Policy",
        document_type="test_policy",
        department="ALL",
        audience="student",
        policy_version="v1",
        effective_from=date(2024, 1, 1),
        effective_to=None,
        visibility="public",
        source_path="data/policies/doc-1.md",
        sections=sections,
    )
    defaults.update(overrides)
    return PolicyDocument(**defaults)


def test_short_section_is_a_single_chunk() -> None:
    document = _make_document(
        [DocumentSection(heading="Overview", body="A short, coherent paragraph.", order=0)]
    )
    chunks = chunk_document(document)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.section == "Overview"
    assert chunk.text == "A short, coherent paragraph."
    assert chunk.chunk_index == 0
    assert chunk.chunk_id == "doc-1::chunk::0"


def test_chunk_preserves_all_metadata() -> None:
    document = _make_document(
        [DocumentSection(heading="Rules", body="Body text.", order=0)],
        policy_version="v2",
        effective_to=date(2025, 12, 31),
        department="CSE",
        audience="staff",
        visibility="admin_only",
    )
    chunk = chunk_document(document)[0]

    assert chunk.document_id == document.document_id
    assert chunk.title == document.title
    assert chunk.document_type == document.document_type
    assert chunk.department == "CSE"
    assert chunk.audience == "staff"
    assert chunk.policy_version == "v2"
    assert chunk.effective_from == document.effective_from
    assert chunk.effective_to == date(2025, 12, 31)
    assert chunk.visibility == "admin_only"
    assert chunk.source == document.source_path


def test_multiple_short_sections_stay_separate_chunks() -> None:
    document = _make_document(
        [
            DocumentSection(heading="First", body="First body.", order=0),
            DocumentSection(heading="Second", body="Second body.", order=1),
        ]
    )
    chunks = chunk_document(document)

    assert [c.section for c in chunks] == ["First", "Second"]
    assert [c.chunk_index for c in chunks] == [0, 1]
    assert [c.chunk_id for c in chunks] == ["doc-1::chunk::0", "doc-1::chunk::1"]


def test_long_section_is_split_on_paragraph_boundaries() -> None:
    paragraphs = [f"Paragraph {i} with enough text to add real length to the section body." for i in range(20)]
    long_body = "\n\n".join(paragraphs)
    document = _make_document([DocumentSection(heading="Long Section", body=long_body, order=0)])

    chunks = chunk_document(document, max_chars=200)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.section.startswith("Long Section (part ")
    # No paragraph's text should have been dropped or duplicated.
    reconstructed = "\n\n".join(c.text for c in chunks)
    for paragraph in paragraphs:
        assert paragraph in reconstructed
    # chunk_index is contiguous and chunk_id matches it.
    indexes = [c.chunk_index for c in chunks]
    assert indexes == list(range(len(chunks)))
    for c in chunks:
        assert c.chunk_id == f"doc-1::chunk::{c.chunk_index}"


def test_chunk_index_is_contiguous_across_sections() -> None:
    long_body = "\n\n".join(f"Paragraph {i} of the long section." for i in range(15))
    document = _make_document(
        [
            DocumentSection(heading="Short", body="Short body.", order=0),
            DocumentSection(heading="Long", body=long_body, order=1),
            DocumentSection(heading="Trailer", body="Trailing short section.", order=2),
        ]
    )
    chunks = chunk_document(document, max_chars=150)

    indexes = [c.chunk_index for c in chunks]
    assert indexes == list(range(len(chunks)))
    assert chunks[0].section == "Short"
    assert chunks[-1].section == "Trailer"
    # The trailer's chunk_id reflects its position after however many parts
    # the long section split into, not a fixed offset.
    assert chunks[-1].chunk_id == f"doc-1::chunk::{len(chunks) - 1}"


def test_chunking_real_corpus_produces_chunks_for_every_document(policy_dir: Path) -> None:
    documents = load_policy_documents(policy_dir)
    for document in documents:
        chunks = chunk_document(document)
        assert len(chunks) >= len(document.sections)
        for chunk in chunks:
            assert chunk.document_id == document.document_id
            assert chunk.text.strip() != ""
