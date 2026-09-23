"""Career Agent vertical-slice schemas (Phase 6).

Mirrors app/schemas/academic.py's three-family structure: service DTOs
(read-only data from app/services/career.py), rule outputs (from
app/rules/opportunity_eligibility.py), and LLM/agent boundary models.

Mandatory vs. recommended skills: ``RequiredSkillSummary.minimum_proficiency``
being ``None`` means the skill is *recommended* (listed as relevant, no
numeric bar to enforce); a non-``None`` value means it's *mandatory* (the
student must have the skill at or above that proficiency to be eligible).
This reuses the already-optional ``OpportunitySkill.minimum_proficiency``
column as-is -- no schema change, and every currently-seeded skill
requirement already specifies a number, so this reinterpretation changes no
existing eligibility outcome.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import NonBlankStr
from app.schemas.enums import VerificationStatus
from app.schemas.evidence import Evidence

# ---------------------------------------------------------------------------
# Career intent (LLM classification boundary)
# ---------------------------------------------------------------------------


class CareerIntent(str, Enum):
    """The supported categories of career request the LLM may classify a query into."""

    OPPORTUNITY_DISCOVERY = "opportunity_discovery"
    APPLICATION_STATUS = "application_status"
    POLICY_QUESTION = "policy_question"
    UNKNOWN = "unknown"


class CareerIntentResult(BaseModel):
    """Structured output of ``LLMProvider.classify_career_intent``."""

    intent: CareerIntent


# ---------------------------------------------------------------------------
# Service DTOs (app/services/career.py)
# ---------------------------------------------------------------------------


class StudentSkillSummary(BaseModel):
    name: NonBlankStr
    proficiency: int = Field(ge=1, le=5)


class RequiredSkillSummary(BaseModel):
    """A skill an opportunity lists. ``minimum_proficiency=None`` => recommended, not mandatory."""

    name: NonBlankStr
    minimum_proficiency: Optional[int] = Field(default=None, ge=1, le=5)


class OpportunitySummary(BaseModel):
    opportunity_id: int
    title: NonBlankStr
    company: NonBlankStr
    opportunity_type: NonBlankStr
    minimum_cgpa: float
    deadline: datetime
    status: NonBlankStr
    allowed_departments: List[str] = Field(default_factory=list)
    eligible_years: List[int] = Field(default_factory=list)
    required_skills: List[RequiredSkillSummary] = Field(default_factory=list)


class ApplicationSummary(BaseModel):
    opportunity_title: NonBlankStr
    status: NonBlankStr


class CareerProfile(BaseModel):
    student_code: NonBlankStr
    full_name: NonBlankStr
    department_code: NonBlankStr
    year: int
    cgpa: float
    skills: List[StudentSkillSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Deterministic eligibility (app/rules/opportunity_eligibility.py)
# ---------------------------------------------------------------------------


class OpportunityEligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"


class OpportunityEligibility(BaseModel):
    """Deterministic eligibility result for one (student, opportunity) pair."""

    status: OpportunityEligibilityStatus
    opportunity: OpportunitySummary
    blocking_reasons: List[str] = Field(default_factory=list)
    missing_mandatory_skills: List[str] = Field(default_factory=list)
    missing_recommended_skills: List[str] = Field(default_factory=list)
    already_applied: bool = False
    application_status: Optional[str] = None


# ---------------------------------------------------------------------------
# Final response generation (app/llm/base.py)
# ---------------------------------------------------------------------------


class CareerResponseContext(BaseModel):
    """Everything ``LLMProvider.generate_career_response`` may read.

    Assembled only from already-verified structured data, same contract as
    ``app.schemas.academic.AcademicResponseContext``.
    """

    intent: CareerIntent
    verification_status: VerificationStatus
    verification_issues: List[str] = Field(default_factory=list)
    student_name: Optional[str] = None
    eligibilities: List[OpportunityEligibility] = Field(default_factory=list)
    skill_gaps: List[str] = Field(default_factory=list)
    applications: List[ApplicationSummary] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
