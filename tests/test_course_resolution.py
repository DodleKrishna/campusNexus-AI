"""Tests for deterministic course resolution (app/agents/academic/course_resolution.py)."""
from __future__ import annotations

import pytest

from app.agents.academic.course_resolution import resolve_course
from app.schemas.academic import CourseResolutionStatus, CourseSummary

COURSES = [
    CourseSummary(course_code="CS301", title="Operating Systems", credits=4, semester=5, instructor="Dr. Manoj Pillai"),
    CourseSummary(course_code="CS302", title="Database Management Systems", credits=4, semester=5, instructor="Dr. Sunita Rao"),
    CourseSummary(course_code="CS303", title="Computer Networks", credits=3, semester=5, instructor="Dr. Ashok Verma"),
    CourseSummary(course_code="CS304", title="Software Engineering", credits=3, semester=5, instructor="Dr. Leela Nair"),
]


@pytest.mark.parametrize("reference", ["CS301", "cs301", "Operating Systems", "operating systems", "OS", "os"])
def test_resolves_operating_systems(reference: str) -> None:
    result = resolve_course(reference, COURSES)
    assert result.status == CourseResolutionStatus.RESOLVED
    assert result.course_code == "CS301"


def test_resolves_by_acronym_for_multiword_title() -> None:
    result = resolve_course("DMS", COURSES)
    assert result.status == CourseResolutionStatus.RESOLVED
    assert result.course_code == "CS302"


def test_unknown_course_reference() -> None:
    result = resolve_course("Blockchain", COURSES)
    assert result.status == CourseResolutionStatus.UNKNOWN
    assert result.course_code is None


def test_ambiguous_reference_matches_multiple_titles() -> None:
    result = resolve_course("Systems", COURSES)
    assert result.status == CourseResolutionStatus.AMBIGUOUS
    assert set(result.candidates) == {"CS301", "CS302"}


@pytest.mark.parametrize("reference", [None, "", "   "])
def test_no_reference_is_not_requested(reference) -> None:
    result = resolve_course(reference, COURSES)
    assert result.status == CourseResolutionStatus.NOT_REQUESTED


def test_short_unmatched_reference_is_unknown_not_a_loose_substring_guess() -> None:
    result = resolve_course("CS", COURSES)
    assert result.status == CourseResolutionStatus.UNKNOWN


def test_never_fabricates_a_course_not_in_enrolled_list() -> None:
    result = resolve_course("CS999", COURSES)
    assert result.status == CourseResolutionStatus.UNKNOWN
    assert result.course_code is None
    assert result.course_code not in {c.course_code for c in COURSES}
