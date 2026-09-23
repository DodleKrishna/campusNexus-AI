"""Campus Services Agent vertical-slice schemas (Phase 6). Mirrors app/schemas/academic.py."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import NonBlankStr
from app.schemas.enums import VerificationStatus
from app.schemas.evidence import Evidence

# ---------------------------------------------------------------------------
# Services intent (LLM classification boundary)
# ---------------------------------------------------------------------------


class ServicesIntent(str, Enum):
    """The supported categories of campus-services request the LLM may classify a query into."""

    CASE_STATUS = "case_status"
    POLICY_QUESTION = "policy_question"
    UNKNOWN = "unknown"


class ServicesIntentResult(BaseModel):
    """Structured output of ``LLMProvider.classify_services_intent``."""

    intent: ServicesIntent


# ---------------------------------------------------------------------------
# Service DTOs (app/services/services.py)
# ---------------------------------------------------------------------------


class CaseSummary(BaseModel):
    case_code: NonBlankStr
    student_code: NonBlankStr
    category: NonBlankStr
    description: NonBlankStr
    priority: NonBlankStr
    department: NonBlankStr
    status: NonBlankStr
    created_at: datetime


# ---------------------------------------------------------------------------
# Deterministic SLA (app/rules/sla.py)
# ---------------------------------------------------------------------------


class CaseSLAAssessment(BaseModel):
    """Deterministic SLA breach assessment for one case.

    A breach is a missed deadline, full stop -- it does not "un-happen" if
    the case is later responded to/resolved late; ``response_breached``/
    ``resolution_breached`` reflect whether the *due date* was missed,
    checked against the actual response/resolution timestamp when one
    exists, or against "now" when it doesn't.
    """

    case: CaseSummary
    response_due_at: datetime
    resolution_due_at: datetime
    responded_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    response_breached: bool
    resolution_breached: bool


# ---------------------------------------------------------------------------
# Final response generation (app/llm/base.py)
# ---------------------------------------------------------------------------


class ServicesResponseContext(BaseModel):
    intent: ServicesIntent
    verification_status: VerificationStatus
    verification_issues: List[str] = Field(default_factory=list)
    student_name: Optional[str] = None
    case_assessments: List[CaseSLAAssessment] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
