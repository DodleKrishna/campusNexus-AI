"""Deterministic Verifier pre-action/post-action check result schema.

Produced by plain-Python rule modules under app/rules/ (invoked by the
Verifier), never by an LLM call.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import NonBlankStr, utc_now
from app.schemas.enums import VerificationPhase, VerificationStatus


class VerificationCheck(BaseModel):
    """A single named rule check performed as part of a VerificationResult."""

    name: NonBlankStr
    passed: bool
    detail: Optional[str] = None


class VerificationResult(BaseModel):
    """The outcome of a Verifier pre-action or post-action pass over a mission task."""

    verification_id: NonBlankStr
    mission_id: NonBlankStr
    task_id: NonBlankStr
    phase: VerificationPhase
    status: VerificationStatus
    checks: List[VerificationCheck] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)
    evidence_refs: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
