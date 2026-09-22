"""Semantic (heading-based) chunking of parsed policy documents.

Chunks split on section boundaries first; a section is only further split on
paragraph boundaries if it exceeds ``max_chars``, so a short coherent section
never gets chopped. Every chunk carries the full metadata needed to become an
Evidence citation later (document_id, section, version, dates, visibility,
source, chunk_index).
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field

from app.rag.documents import DocumentSection, PolicyDocument
from app.schemas.common import NonBlankStr

MAX_CHUNK_CHARS = 1000


class DocumentChunk(BaseModel):
    """One retrievable unit: a section (or part of a long section) plus metadata."""

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
    chunk_index: int = Field(ge=0)
    source: NonBlankStr
    text: NonBlankStr


def _split_paragraphs(body: str) -> List[str]:
    return [p.strip() for p in body.split("\n\n") if p.strip()]


def _pack_paragraphs(paragraphs: List[str], max_chars: int) -> List[str]:
    """Greedily pack paragraphs into chunks no longer than ``max_chars`` where possible."""
    packed: List[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > max_chars and current:
            packed.append(current)
            current = para
        else:
            current = candidate
    if current:
        packed.append(current)
    return packed


def chunk_section(
    document: PolicyDocument,
    section: DocumentSection,
    *,
    start_index: int,
    max_chars: int = MAX_CHUNK_CHARS,
) -> List[DocumentChunk]:
    if len(section.body) <= max_chars:
        parts = [section.body]
    else:
        parts = _pack_paragraphs(_split_paragraphs(section.body), max_chars) or [section.body]

    multi = len(parts) > 1
    chunks: List[DocumentChunk] = []
    for offset, part in enumerate(parts):
        idx = start_index + offset
        section_label = f"{section.heading} (part {offset + 1})" if multi else section.heading
        chunks.append(
            DocumentChunk(
                chunk_id=f"{document.document_id}::chunk::{idx}",
                document_id=document.document_id,
                title=document.title,
                section=section_label,
                document_type=document.document_type,
                department=document.department,
                audience=document.audience,
                policy_version=document.policy_version,
                effective_from=document.effective_from,
                effective_to=document.effective_to,
                visibility=document.visibility,
                chunk_index=idx,
                source=document.source_path,
                text=part,
            )
        )
    return chunks


def chunk_document(document: PolicyDocument, *, max_chars: int = MAX_CHUNK_CHARS) -> List[DocumentChunk]:
    """Chunk every section of ``document`` in order, with stable chunk_ids/indexes."""
    chunks: List[DocumentChunk] = []
    index = 0
    for section in document.sections:
        section_chunks = chunk_section(document, section, start_index=index, max_chars=max_chars)
        chunks.extend(section_chunks)
        index += len(section_chunks)
    return chunks
