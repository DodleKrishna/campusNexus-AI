"""Tests for the deterministic CareerVerifier (app/verification/career.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas.career import (
    CareerIntent,
    CareerProfile,
    OpportunityEligibility,
    OpportunityEligibilityStatus,
    OpportunitySummary,
)
from app.schemas.enums import VerificationStatus
from app.schemas.evidence import Evidence
from app.verification.career import CareerVerificationInput, CareerVerifier

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)

STUDENT = CareerProfile(student_code="STU-DEMO-001", full_name="Aditi Rao", department_code="CSE", year=3, cgpa=7.8, skills=[])

OPPORTUNITY = OpportunitySummary(
    opportunity_id=1, title="AI Intern", company="NimbusCloud", opportunity_type="internship",
    minimum_cgpa=7.0, deadline=NOW + timedelta(days=10), status="open",
    allowed_departments=["CSE"], eligible_years=[3], required_skills=[],
)

ELIGIBLE = OpportunityEligibility(status=OpportunityEligibilityStatus.ELIGIBLE, opportunity=OPPORTUNITY, blocking_reasons=[])
NOT_ELIGIBLE = OpportunityEligibility(
    status=OpportunityEligibilityStatus.NOT_ELIGIBLE, opportunity=OPPORTUNITY, blocking_reasons=["CGPA too low"]
)

EVIDENCE = [
    Evidence(evidence_id="ev-1", document_id="internship-policy", title="Internship Policy", source="x", snippet="snippet")
]

verifier = CareerVerifier()


def _base_input(**overrides) -> CareerVerificationInput:
    defaults = dict(
        mission_id="m1", task_id="t1", intent=CareerIntent.OPPORTUNITY_DISCOVERY, student=STUDENT,
        requires_opportunities=True, requires_policy_evidence=True, eligibilities=[ELIGIBLE], evidence=EVIDENCE,
    )
    defaults.update(overrides)
    return CareerVerificationInput(**defaults)


def test_fully_verified_case() -> None:
    result = verifier.verify(_base_input())
    assert result.status == VerificationStatus.VERIFIED


def test_missing_student_is_failed() -> None:
    result = verifier.verify(_base_input(student=None))
    assert result.status == VerificationStatus.FAILED


def test_unsupported_intent_is_failed() -> None:
    result = verifier.verify(
        _base_input(intent=CareerIntent.UNKNOWN, requires_opportunities=False, requires_policy_evidence=False, eligibilities=[], evidence=[])
    )
    assert result.status == VerificationStatus.FAILED


def test_no_opportunities_found_is_needs_review() -> None:
    result = verifier.verify(_base_input(eligibilities=[]))
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_missing_evidence_is_needs_review() -> None:
    result = verifier.verify(_base_input(evidence=[]))
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_inconsistent_eligibility_status_is_failed() -> None:
    """status=ELIGIBLE but blocking_reasons non-empty -- an internal contradiction."""
    broken = ELIGIBLE.model_copy(update={"blocking_reasons": ["should not be eligible"]})
    result = verifier.verify(_base_input(eligibilities=[broken]))
    assert result.status == VerificationStatus.FAILED


def test_not_eligible_with_reasons_is_consistent_and_verified() -> None:
    result = verifier.verify(_base_input(eligibilities=[NOT_ELIGIBLE]))
    assert result.status == VerificationStatus.VERIFIED


def test_application_status_intent_does_not_require_opportunities_or_evidence() -> None:
    result = verifier.verify(
        _base_input(
            intent=CareerIntent.APPLICATION_STATUS, requires_opportunities=False, requires_policy_evidence=False,
            eligibilities=[], evidence=[],
        )
    )
    assert result.status == VerificationStatus.VERIFIED
