"""Deterministic course-reference resolution against a student's real enrollments.

The LLM may propose a course reference from the query text (see
app.llm.providers.mock / anthropic_provider), but it never gets to assert a
course exists. This module is the only thing that turns that proposal into a
real, enrolled ``CourseSummary`` -- or flags it ``UNKNOWN``/``AMBIGUOUS`` --
so the agent never fabricates a course.
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.schemas.academic import CourseResolution, CourseResolutionStatus, CourseSummary

_ACRONYM_STOPWORDS = {"of", "and", "the", "for", "in", "to"}
_MIN_SUBSTRING_LENGTH = 4


def _title_acronym(title: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", title) if w.lower() not in _ACRONYM_STOPWORDS]
    return "".join(w[0] for w in words).upper()


def resolve_course(reference: Optional[str], enrolled_courses: List[CourseSummary]) -> CourseResolution:
    """Resolve ``reference`` (a raw string proposed by the LLM) against ``enrolled_courses``.

    Resolution order: exact course code, exact title, exact title-acronym
    (e.g. "OS" for "Operating Systems"), then substring-on-title (only for
    references of at least ``_MIN_SUBSTRING_LENGTH`` characters, to avoid
    over-matching short strings). A substring match against more than one
    course's title is AMBIGUOUS, never guessed.
    """
    if reference is None or not reference.strip():
        return CourseResolution(status=CourseResolutionStatus.NOT_REQUESTED)

    normalized = reference.strip().lower()

    for course in enrolled_courses:
        if normalized == course.course_code.lower():
            return CourseResolution(
                status=CourseResolutionStatus.RESOLVED, course_code=course.course_code, query_reference=reference
            )
    for course in enrolled_courses:
        if normalized == course.title.lower():
            return CourseResolution(
                status=CourseResolutionStatus.RESOLVED, course_code=course.course_code, query_reference=reference
            )
    for course in enrolled_courses:
        if normalized == _title_acronym(course.title).lower():
            return CourseResolution(
                status=CourseResolutionStatus.RESOLVED, course_code=course.course_code, query_reference=reference
            )

    if len(normalized) >= _MIN_SUBSTRING_LENGTH:
        seen: set[str] = set()
        matches = [
            course.course_code
            for course in enrolled_courses
            if normalized in course.title.lower() and not (course.course_code in seen or seen.add(course.course_code))
        ]
        if len(matches) == 1:
            return CourseResolution(
                status=CourseResolutionStatus.RESOLVED, course_code=matches[0], query_reference=reference
            )
        if len(matches) > 1:
            return CourseResolution(
                status=CourseResolutionStatus.AMBIGUOUS, candidates=matches, query_reference=reference
            )

    return CourseResolution(status=CourseResolutionStatus.UNKNOWN, query_reference=reference)
