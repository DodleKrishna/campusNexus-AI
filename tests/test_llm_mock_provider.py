"""Tests for the deterministic MockLLMProvider (app/llm/providers/mock.py).

These are the provider-level unit tests; tests/test_academic_agent.py covers
the same provider wired into the full agent flow.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.llm.providers.mock import MockLLMProvider
from app.schemas.academic import (
    AcademicIntent,
    AcademicResponseContext,
    AttendanceCalculation,
    AttendanceRuleStatus,
    CourseResolution,
    CourseResolutionStatus,
    CourseSummary,
)
from app.schemas.enums import VerificationStatus

COURSES = [
    CourseSummary(course_code="CS301", title="Operating Systems", credits=4, semester=5, instructor="Dr. Manoj Pillai"),
    CourseSummary(course_code="CS302", title="Database Management Systems", credits=4, semester=5, instructor="Dr. Sunita Rao"),
]

provider = MockLLMProvider()


@pytest.mark.parametrize(
    "query,expected_intent",
    [
        ("What's my attendance in Operating Systems?", AcademicIntent.ATTENDANCE_STATUS),
        ("What's my attendance in OS?", AcademicIntent.ATTENDANCE_STATUS),
        ("Can I write my OS exam?", AcademicIntent.EXAM_ELIGIBILITY),
        ("How many classes do I need to attend in OS to reach the required attendance?", AcademicIntent.ATTENDANCE_RECOVERY),
        ("What is my timetable?", AcademicIntent.TIMETABLE),
        ("When are my exams?", AcademicIntent.EXAM_SCHEDULE),
        ("What is the make-up class policy?", AcademicIntent.POLICY_QUESTION),
        ("Can you order me a pizza?", AcademicIntent.UNKNOWN),
    ],
)
def test_classify_academic_intent(query: str, expected_intent: AcademicIntent) -> None:
    result = provider.classify_academic_intent(query, COURSES)
    assert result.intent == expected_intent


def test_classify_extracts_course_code() -> None:
    result = provider.classify_academic_intent("What's my attendance in CS301?", COURSES)
    assert result.raw_course_reference == "CS301"


def test_classify_extracts_acronym_for_unnamed_course() -> None:
    result = provider.classify_academic_intent("What's my attendance in OS?", COURSES)
    assert result.raw_course_reference == "OS"


def test_classify_extracts_generic_phrase_for_unknown_course() -> None:
    result = provider.classify_academic_intent("What's my attendance in Blockchain?", COURSES)
    assert result.raw_course_reference == "Blockchain"


def test_classify_no_course_reference_when_none_present() -> None:
    result = provider.classify_academic_intent("What is my timetable?", COURSES)
    assert result.raw_course_reference is None


def _attendance_context(**overrides) -> AcademicResponseContext:
    attendance = AttendanceCalculation(
        status=AttendanceRuleStatus.OK,
        classes_attended=34,
        classes_conducted=50,
        required_percentage=Decimal("75"),
        current_percentage=Decimal("68.0"),
        eligible_now=False,
        classes_needed_to_reach_threshold=14,
        threshold_reachable=True,
    )
    defaults = dict(
        intent=AcademicIntent.ATTENDANCE_STATUS,
        verification_status=VerificationStatus.VERIFIED,
        student_name="Aditi Rao",
        course=COURSES[0],
        course_resolution=CourseResolution(status=CourseResolutionStatus.RESOLVED, course_code="CS301"),
        attendance=attendance,
    )
    defaults.update(overrides)
    return AcademicResponseContext(**defaults)


def test_response_reports_percentage_and_recovery_path() -> None:
    text = provider.generate_academic_response(_attendance_context())
    assert "68.0%" in text
    assert "75%" in text
    assert "14" in text


def test_response_hedges_under_needs_review() -> None:
    text = provider.generate_academic_response(_attendance_context(verification_status=VerificationStatus.NEEDS_REVIEW))
    assert text.lower().startswith("note:")


def test_response_never_asserts_facts_when_failed() -> None:
    context = AcademicResponseContext(
        intent=AcademicIntent.ATTENDANCE_STATUS,
        verification_status=VerificationStatus.FAILED,
        student_name=None,
    )
    text = provider.generate_academic_response(context)
    assert "68" not in text
    assert "couldn't find a student record" in text


def test_response_surfaces_ambiguous_course_clarification() -> None:
    context = _attendance_context(
        course_resolution=CourseResolution(
            status=CourseResolutionStatus.AMBIGUOUS, candidates=["CS301", "CS302"], query_reference="Systems"
        ),
        attendance=None,
        verification_status=VerificationStatus.NEEDS_REVIEW,
    )
    text = provider.generate_academic_response(context)
    assert "CS301" in text and "CS302" in text
    assert "Systems" in text
