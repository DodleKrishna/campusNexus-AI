"""Tests for the deterministic ServicesVerifier (app/verification/services.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas.enums import VerificationStatus
from app.schemas.evidence import Evidence
from app.schemas.services import CaseSLAAssessment, CaseSummary, ServicesIntent
from app.verification.services import ServicesVerificationInput, ServicesVerifier

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)

CASE = CaseSummary(
    case_code="CASE-0001", student_code="STU-DEMO-001", category="hostel", description="d",
    priority="normal", department="Hostel Office", status="open", created_at=NOW - timedelta(days=1),
)

ASSESSMENT = CaseSLAAssessment(
    case=CASE, response_due_at=NOW + timedelta(hours=10), resolution_due_at=NOW + timedelta(hours=100),
    responded_at=None, resolved_at=None, response_breached=False, resolution_breached=False,
)

EVIDENCE = [Evidence(evidence_id="ev-1", document_id="grievance-sla-policy", title="Grievance SLA", source="x", snippet="s")]

verifier = ServicesVerifier()


def _base_input(**overrides) -> ServicesVerificationInput:
    defaults = dict(
        mission_id="m1", task_id="t1", intent=ServicesIntent.CASE_STATUS, student_exists=True,
        expected_student_code="STU-DEMO-001", requires_cases=True, requires_policy_evidence=True, now=NOW,
        case_assessments=[ASSESSMENT], cases_missing_sla=[], evidence=EVIDENCE,
    )
    defaults.update(overrides)
    return ServicesVerificationInput(**defaults)


def test_fully_verified_case() -> None:
    result = verifier.verify(_base_input())
    assert result.status == VerificationStatus.VERIFIED


def test_missing_student_is_failed() -> None:
    result = verifier.verify(_base_input(student_exists=False, case_assessments=[], evidence=[]))
    assert result.status == VerificationStatus.FAILED


def test_unsupported_intent_is_failed() -> None:
    result = verifier.verify(
        _base_input(intent=ServicesIntent.UNKNOWN, requires_cases=False, requires_policy_evidence=False, case_assessments=[], evidence=[])
    )
    assert result.status == VerificationStatus.FAILED


def test_case_owned_by_a_different_student_is_failed() -> None:
    other_case = CASE.model_copy(update={"student_code": "STU2023002"})
    broken = ASSESSMENT.model_copy(update={"case": other_case})
    result = verifier.verify(_base_input(case_assessments=[broken]))
    assert result.status == VerificationStatus.FAILED


def test_inconsistent_sla_flag_is_failed() -> None:
    """response_breached=True stored, but recomputing from the due dates says False."""
    broken = ASSESSMENT.model_copy(update={"response_breached": True})
    result = verifier.verify(_base_input(case_assessments=[broken]))
    assert result.status == VerificationStatus.FAILED


def test_missing_sla_record_is_needs_review() -> None:
    result = verifier.verify(_base_input(case_assessments=[ASSESSMENT], cases_missing_sla=["CASE-0002"]))
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_missing_evidence_is_needs_review() -> None:
    result = verifier.verify(_base_input(evidence=[]))
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_breached_case_with_correct_flags_is_still_verified() -> None:
    breached = ASSESSMENT.model_copy(
        update={"response_breached": True, "response_due_at": NOW - timedelta(hours=1)}
    )
    result = verifier.verify(_base_input(case_assessments=[breached]))
    assert result.status == VerificationStatus.VERIFIED
