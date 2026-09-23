"""Read-only academic repository functions."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.academic import AttendanceRecord, Course, Enrollment, Exam, TimetableSlot
from app.db.models.identity import Student


def get_enrollment(session: Session, student_code: str, course_code: str) -> Optional[Enrollment]:
    """A student's enrollment in a specific course, or None if not enrolled.

    ``course_code`` matching is case-insensitive since it is typically echoed
    back from a natural-language query.
    """
    stmt = (
        select(Enrollment)
        .join(Student, Enrollment.student_id == Student.id)
        .join(Course, Enrollment.course_id == Course.id)
        .where(Student.student_code == student_code, Course.code == course_code.upper())
    )
    return session.execute(stmt).scalar_one_or_none()


def get_student_courses(session: Session, student_code: str) -> list[Enrollment]:
    """All enrollments (any academic year/semester) for a student."""
    stmt = (
        select(Enrollment)
        .join(Student, Enrollment.student_id == Student.id)
        .where(Student.student_code == student_code)
    )
    return list(session.execute(stmt).scalars().all())


def get_attendance_records(session: Session, student_code: str) -> list[AttendanceRecord]:
    """Raw attendance counters for every enrollment of a student."""
    stmt = (
        select(AttendanceRecord)
        .join(Enrollment, AttendanceRecord.enrollment_id == Enrollment.id)
        .join(Student, Enrollment.student_id == Student.id)
        .where(Student.student_code == student_code)
    )
    return list(session.execute(stmt).scalars().all())


def get_timetable(session: Session, student_code: str) -> list[TimetableSlot]:
    """Timetable slots for the courses a student is currently enrolled in."""
    stmt = (
        select(TimetableSlot)
        .join(Enrollment, TimetableSlot.course_id == Enrollment.course_id)
        .join(Student, Enrollment.student_id == Student.id)
        .where(Student.student_code == student_code)
    )
    return list(session.execute(stmt).scalars().all())


def get_exam_schedule(session: Session, student_code: str) -> list[Exam]:
    """Exams for the courses a student is currently enrolled in."""
    stmt = (
        select(Exam)
        .join(Enrollment, Exam.course_id == Enrollment.course_id)
        .join(Student, Enrollment.student_id == Student.id)
        .where(Student.student_code == student_code)
    )
    return list(session.execute(stmt).scalars().all())
