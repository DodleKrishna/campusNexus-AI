"""Structured RAG citation schema returned by the Knowledge/RAG Agent.

Every factual/policy claim other agents rely on must be grounded in an Evidence
instance so the Verifier and the user can trace the claim to its source
(CLAUDE.md RAG / Evidence Rules).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.common import NonBlankStr


class Evidence(BaseModel):
    """A single retrieved citation grounding a factual or policy claim."""

    evidence_id: NonBlankStr
    document_id: NonBlankStr
    title: NonBlankStr
    source: NonBlankStr
    snippet: NonBlankStr
    section: Optional[str] = None
    page: Optional[int] = Field(default=None, ge=0)
    policy_version: Optional[str] = None
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    relevance_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
