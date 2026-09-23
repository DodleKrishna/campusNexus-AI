"""Tests for the deterministic AcademicVerifier (app/verification/academic.py)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.schemas.academic import (
    AcademicIntent,
    AttendanceCalculation,
    AttendanceRuleStatus,
    CourseResolution,
    CourseResolutionStatus,
    ExamEligibilityResult,
    ExamEligibilityStatus,
    PolicyThreshold,
    StudentAcademicProfile,
    ThresholdExtractionStatus,
)
from app.schemas.enums import VerificationStatus
from app.schemas.evidence import Evidence
from app.verification.academic import AcademicVerificationInput, AcademicVerifier

STUDENT = StudentAcademicProfile(
    student_code="STU-DEMO-001", full_name="Aditi Rao", department_code="CSE", year=3, semester=5, cgpa=7.8, courses=[]
)

VALID_ATTENDANCE = AttendanceCalculation(
    status=AttendanceRuleStatus.OK,
    classes_attended=34,
    classes_conducted=50,
    required_percentage=Decimal("75"),
    current_percentage=Decimal("68.0"),
    eligible_now=False,
    classes_needed_to_reach_threshold=14,
    threshold_reachable=True,
)

VALID_THRESHOLD = PolicyThreshold(
    status=ThresholdExtractionStatus.OK,
    required_percentage=Decimal("75"),
    document_id="attendance-policy-v2",
    policy_version="v2",
    section="Minimum Attendance Requirement",
)

EVIDENCE = [
    Evidence(
        evidence_id="ev-1",
        document_id="attendance-policy-v2",
        title="Attendance Policy",
        source="data/policies/attendance_policy_v2.md",
        snippet="Students must maintain a minimum of 75% attendance.",
        section="Minimum Attendance Requirement",
        policy_version="v2",
        effective_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
    )
]

verifier = AcademicVerifier()


def _base_input(**overrides) -> AcademicVerificationInput:
    defaults = dict(
        mission_id="m1",
        task_id="t1",
        intent=AcademicIntent.ATTENDANCE_STATUS,
        student=STUDENT,
        requires_course=True,
        requires_attendance=True,
        requires_policy_evidence=True,
        course_resolution=CourseResolution(status=CourseResolutionStatus.RESOLVED, course_code="CS301"),
        attendance=VALID_ATTENDANCE,
        threshold=VALID_THRESHOLD,
        evidence=EVIDENCE,
    )
    defaults.update(overrides)
    return AcademicVerificationInput(**defaults)


def test_fully_verified_case() -> None:
    result = verifier.verify(_base_input())
    assert result.status == VerificationStatus.VERIFIED
    assert all(check.passed for check in result.checks)


def test_missing_student_is_failed() -> None:
    result = verifier.verify(_base_input(student=None, course_resolution=None))
    assert result.status == VerificationStatus.FAILED
    assert any("Student record not found" in issue for issue in result.issues)


def test_unknown_course_is_failed() -> None:
    result = verifier.verify(
        _base_input(
            course_resolution=CourseResolution(status=CourseResolutionStatus.UNKNOWN, query_reference="Blockchain"),
            attendance=None,
            threshold=None,
            evidence=[],
        )
    )
    assert result.status == VerificationStatus.FAILED


def test_ambiguous_course_is_needs_review_not_failed() -> None:
    result = verifier.verify(
        _base_input(
            course_resolution=CourseResolution(
                status=CourseResolutionStatus.AMBIGUOUS, candidates=["CS301", "CS302"], query_reference="Systems"
            ),
            attendance=None,
            threshold=None,
            evidence=[],
        )
    )
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_ambiguous_threshold_is_needs_review() -> None:
    result = verifier.verify(
        _base_input(threshold=PolicyThreshold(status=ThresholdExtractionStatus.AMBIGUOUS), attendance=None)
    )
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_threshold_not_found_is_needs_review() -> None:
    result = verifier.verify(
        _base_input(threshold=PolicyThreshold(status=ThresholdExtractionStatus.NOT_FOUND), attendance=None)
    )
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_invalid_attendance_source_is_failed() -> None:
    invalid = AttendanceCalculation(
        status=AttendanceRuleStatus.INVALID_INPUT,
        classes_attended=10,
        classes_conducted=5,
        required_percentage=Decimal("75"),
    )
    result = verifier.verify(_base_input(attendance=invalid))
    assert result.status == VerificationStatus.FAILED


def test_no_classes_conducted_is_needs_review() -> None:
    pending = AttendanceCalculation(
        status=AttendanceRuleStatus.NO_CLASSES_CONDUCTED,
        classes_attended=0,
        classes_conducted=0,
        required_percentage=Decimal("75"),
    )
    result = verifier.verify(_base_input(attendance=pending))
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_inconsistent_rule_output_is_failed() -> None:
    """eligible_now says True but the raw counters are below the required percentage."""
    inconsistent = VALID_ATTENDANCE.model_copy(update={"eligible_now": True})
    result = verifier.verify(_base_input(attendance=inconsistent))
    assert result.status == VerificationStatus.FAILED
    assert any("consistency" in issue for issue in result.issues) or any(
        "consistency" in (c.detail or "") for c in result.checks
    )


def test_unsupported_intent_is_failed() -> None:
    result = verifier.verify(
        _base_input(
            intent=AcademicIntent.UNKNOWN,
            requires_course=False,
            requires_attendance=False,
            requires_policy_evidence=False,
            course_resolution=None,
            attendance=None,
            threshold=None,
            evidence=[],
        )
    )
    assert result.status == VerificationStatus.FAILED


def test_eligibility_consistent_with_attendance_is_verified() -> None:
    eligibility = ExamEligibilityResult(status=ExamEligibilityStatus.NOT_ELIGIBLE, attendance=VALID_ATTENDANCE)
    result = verifier.verify(_base_input(eligibility=eligibility, intent=AcademicIntent.EXAM_ELIGIBILITY))
    assert result.status == VerificationStatus.VERIFIED


def test_eligibility_inconsistent_with_attendance_is_failed() -> None:
    mismatched = ExamEligibilityResult(status=ExamEligibilityStatus.ELIGIBLE, attendance=VALID_ATTENDANCE)
    result = verifier.verify(_base_input(eligibility=mismatched, intent=AcademicIntent.EXAM_ELIGIBILITY))
    assert result.status == VerificationStatus.FAILED


def test_timetable_intent_does_not_require_course_or_attendance() -> None:
    result = verifier.verify(
        _base_input(
            intent=AcademicIntent.TIMETABLE,
            requires_course=False,
            requires_attendance=False,
            requires_policy_evidence=False,
            course_resolution=CourseResolution(status=CourseResolutionStatus.NOT_REQUESTED),
            attendance=None,
            threshold=None,
            evidence=[],
        )
    )
    assert result.status == VerificationStatus.VERIFIED
