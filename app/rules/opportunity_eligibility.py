"""Deterministic internship/job eligibility (CLAUDE.md: official-rule
calculations are plain Python, never an LLM call).

Mandatory checks: opportunity status, application deadline, student year,
student department, minimum CGPA, and every *mandatory* required skill
(``minimum_proficiency is not None``) met at or above that bar. A
*recommended* skill (``minimum_proficiency is None``) the student lacks is
surfaced as a gap but never blocks eligibility -- CLAUDE.md/Phase 6 spec:
"missing recommended skills should not automatically make a student
ineligible."

``eligible_years``/``allowed_departments`` come directly from their join
tables (already the authoritative per-opportunity source -- unlike the
Academic Agent's attendance threshold, there is no policy-prose-vs-number
gap to bridge here, so no text extraction is needed for these checks).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from app.schemas.career import (
    CareerProfile,
    OpportunityEligibility,
    OpportunityEligibilityStatus,
    OpportunitySummary,
)

_OPEN_STATUS = "open"


def compute_eligibility(
    profile: CareerProfile,
    opportunity: OpportunitySummary,
    *,
    now: datetime,
    already_applied: bool = False,
    application_status: Optional[str] = None,
) -> OpportunityEligibility:
    reasons: List[str] = []

    if opportunity.status != _OPEN_STATUS:
        reasons.append(f"opportunity status is {opportunity.status!r}, not open")
    if now > opportunity.deadline:
        reasons.append(f"application deadline ({opportunity.deadline.isoformat()}) has passed")
    if opportunity.eligible_years and profile.year not in opportunity.eligible_years:
        reasons.append(f"year {profile.year} is not in the eligible years {opportunity.eligible_years}")
    if opportunity.allowed_departments and profile.department_code not in opportunity.allowed_departments:
        reasons.append(
            f"department {profile.department_code!r} is not in the allowed departments "
            f"{opportunity.allowed_departments}"
        )
    if profile.cgpa < opportunity.minimum_cgpa:
        reasons.append(f"CGPA {profile.cgpa} is below the required minimum {opportunity.minimum_cgpa}")

    student_proficiency_by_skill = {s.name.lower(): s.proficiency for s in profile.skills}
    missing_mandatory: List[str] = []
    missing_recommended: List[str] = []
    for requirement in opportunity.required_skills:
        have = student_proficiency_by_skill.get(requirement.name.lower())
        if requirement.minimum_proficiency is None:
            if have is None:
                missing_recommended.append(requirement.name)
        else:
            if have is None or have < requirement.minimum_proficiency:
                missing_mandatory.append(requirement.name)

    if missing_mandatory:
        reasons.append(f"missing mandatory skill(s): {', '.join(missing_mandatory)}")

    status = OpportunityEligibilityStatus.NOT_ELIGIBLE if reasons else OpportunityEligibilityStatus.ELIGIBLE

    return OpportunityEligibility(
        status=status,
        opportunity=opportunity,
        blocking_reasons=reasons,
        missing_mandatory_skills=missing_mandatory,
        missing_recommended_skills=missing_recommended,
        already_applied=already_applied,
        application_status=application_status,
    )
