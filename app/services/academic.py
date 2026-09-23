"""The Academic Service: read-only tools over the Phase 2 academic repositories.

CLAUDE.md draws a hard line between what's deterministic-Python and what an
LLM may touch. This module is firmly on the deterministic side: every
function here is a typed, read-only accessor over ``app/db/repositories/``.
No arbitrary SQL, no LLM calls, no write access -- the Academic Agent (and
nothing else) calls these to gather facts, then hands them to the rule
modules and the LLM response renderer.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.repositories.academics import (
    get_attendance_records,
    get_enrollment,
    get_exam_schedule as _get_exam_schedule,
    get_student_courses,
    get_timetable as _get_timetable,
)
from app.db.repositories.students import get_student_by_id
from app.schemas.academic import (
    AttendanceSnapshot,
    CourseSummary,
    ExamEntry,
    StudentAcademicProfile,
    TimetableEntry,
)


def _course_summary(enrollment) -> CourseSummary:
    course = enrollment.course
    return CourseSummary(
        course_code=course.code,
        title=course.title,
        credits=course.credits,
        semester=course.semester,
        instructor=course.instructor,
    )


def get_student_academic_profile(session: Session, student_id: str) -> Optional[StudentAcademicProfile]:
    """A typed snapshot of the student's identity + current enrollments, or None if unknown."""
    student = get_student_by_id(session, student_id)
    if student is None:
        return None

    enrollments = get_student_courses(session, student_id)
    return StudentAcademicProfile(
        student_code=student.student_code,
        full_name=student.user.full_name,
        department_code=student.department.code,
        year=student.year,
        semester=student.semester,
        cgpa=student.cgpa,
        courses=[_course_summary(e) for e in enrollments],
    )


def get_attendance(
    session: Session, student_id: str, course_code: Optional[str] = None
) -> List[AttendanceSnapshot]:
    """Raw attendance counters for the student, optionally filtered to one course."""
    records = get_attendance_records(session, student_id)
    snapshots = [
        AttendanceSnapshot(
            course_code=record.enrollment.course.code,
            course_title=record.enrollment.course.title,
            classes_attended=record.classes_attended,
            classes_conducted=record.classes_conducted,
        )
        for record in records
    ]
    if course_code is not None:
        normalized = course_code.upper()
        snapshots = [s for s in snapshots if s.course_code.upper() == normalized]
    return snapshots


def get_timetable(session: Session, student_id: str) -> List[TimetableEntry]:
    """Weekly timetable slots for the student's current enrollments."""
    slots = _get_timetable(session, student_id)
    return [
        TimetableEntry(
            course_code=slot.course.code,
            course_title=slot.course.title,
            weekday=slot.weekday,
            start_time=slot.start_time.isoformat(timespec="minutes"),
            end_time=slot.end_time.isoformat(timespec="minutes"),
            location=slot.location,
        )
        for slot in slots
    ]


def get_exam_schedule(session: Session, student_id: str) -> List[ExamEntry]:
    """Scheduled exams for the student's current enrollments."""
    exams = _get_exam_schedule(session, student_id)
    return [
        ExamEntry(
            course_code=exam.course.code,
            course_title=exam.course.title,
            exam_type=exam.exam_type,
            scheduled_start=exam.scheduled_start.isoformat(),
            scheduled_end=exam.scheduled_end.isoformat(),
            location=exam.location,
        )
        for exam in exams
    ]


def get_course(session: Session, student_id: str, course_code: str) -> Optional[CourseSummary]:
    """A single enrolled course by code, or None if the student isn't enrolled in it."""
    enrollment = get_enrollment(session, student_id, course_code)
    if enrollment is None:
        return None
    return _course_summary(enrollment)
