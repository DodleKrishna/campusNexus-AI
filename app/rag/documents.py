"""Policy document model and front-matter/section parsing.

A PolicyDocument is the parsed representation of one Markdown file under
data/policies/: YAML front matter metadata plus a list of heading-delimited
sections. This is internal RAG infrastructure, not the Phase 1 Evidence
boundary model -- KnowledgeService converts retrieval results into Evidence
at the service boundary instead.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator

from app.schemas.common import NonBlankStr

REQUIRED_FRONT_MATTER_FIELDS = (
    "document_id",
    "title",
    "document_type",
    "department",
    "audience",
    "policy_version",
    "effective_from",
    "visibility",
)

KNOWN_VISIBILITY = {"public", "admin_only"}
KNOWN_AUDIENCE = {"student", "staff", "faculty", "all"}

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?", re.DOTALL)
_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class DocumentSection(BaseModel):
    """A single ``##``-delimited section of a policy document, pre-chunking."""

    heading: NonBlankStr
    body: NonBlankStr
    order: int = Field(ge=0)


class PolicyDocument(BaseModel):
    """A parsed policy document: front-matter metadata plus its sections."""

    document_id: NonBlankStr
    title: NonBlankStr
    document_type: NonBlankStr
    department: NonBlankStr
    audience: NonBlankStr
    policy_version: NonBlankStr
    effective_from: date
    effective_to: Optional[date] = None
    visibility: NonBlankStr
    source_path: NonBlankStr
    sections: List[DocumentSection]

    @field_validator("visibility")
    @classmethod
    def _validate_visibility(cls, value: str) -> str:
        if value not in KNOWN_VISIBILITY:
            raise ValueError(f"unknown visibility {value!r}; expected one of {sorted(KNOWN_VISIBILITY)}")
        return value

    @field_validator("audience")
    @classmethod
    def _validate_audience(cls, value: str) -> str:
        if value not in KNOWN_AUDIENCE:
            raise ValueError(f"unknown audience {value!r}; expected one of {sorted(KNOWN_AUDIENCE)}")
        return value

    @field_validator("sections")
    @classmethod
    def _validate_sections(cls, value: List[DocumentSection]) -> List[DocumentSection]:
        if not value:
            raise ValueError("document has no sections to chunk")
        return value


def _parse_front_matter(text: str, *, path: Path) -> Tuple[Dict[str, object], str]:
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        raise ValueError(f"{path}: missing YAML front matter (expected a leading '---' block)")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise ValueError(f"{path}: front matter did not parse to a mapping")
    body = text[match.end():]
    return metadata, body


def _split_sections(body: str, *, path: Path) -> List[DocumentSection]:
    matches = list(_SECTION_HEADING_RE.finditer(body))
    if not matches:
        raise ValueError(f"{path}: no '##' sections found to chunk")

    sections: List[DocumentSection] = []
    for order, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[order + 1].start() if order + 1 < len(matches) else len(body)
        section_body = body[start:end].strip()
        if not section_body:
            continue
        sections.append(DocumentSection(heading=heading, body=section_body, order=order))
    return sections


def parse_policy_document(path: Path, *, source_prefix: str = "data/policies") -> PolicyDocument:
    """Parse a single Markdown policy file into a PolicyDocument."""
    text = path.read_text(encoding="utf-8")
    metadata, body = _parse_front_matter(text, path=path)

    missing = [f for f in REQUIRED_FRONT_MATTER_FIELDS if metadata.get(f) in (None, "")]
    if missing:
        raise ValueError(f"{path}: missing required front matter fields: {missing}")

    sections = _split_sections(body, path=path)

    return PolicyDocument(
        document_id=str(metadata["document_id"]),
        title=str(metadata["title"]),
        document_type=str(metadata["document_type"]),
        department=str(metadata["department"]),
        audience=str(metadata["audience"]),
        policy_version=str(metadata["policy_version"]),
        effective_from=metadata["effective_from"],
        effective_to=metadata.get("effective_to"),
        visibility=str(metadata["visibility"]),
        source_path=f"{source_prefix}/{path.name}",
        sections=sections,
    )


def load_policy_documents(directory: Path, *, source_prefix: str = "data/policies") -> List[PolicyDocument]:
    """Parse every ``*.md`` file in ``directory`` into a PolicyDocument, sorted by filename."""
    directory = Path(directory)
    return [
        parse_policy_document(path, source_prefix=source_prefix)
        for path in sorted(directory.glob("*.md"))
    ]
