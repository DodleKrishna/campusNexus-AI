"""Read-only academic repository functions."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from app.db.models.academic import AttendanceRecord, Course, Enrollment, Exam, ExamStatus, TimetableSlot
from app.db.models.exam import ExamTarget
from app.db.models.identity import Student

# Phase 4: a managed exam (``teaching_assignment_id`` set) is on a student's schedule only once it is scheduled and
# the student is one of its snapshotted targets. Timetable-only exams (no class) are unchanged.
_LIVE_EXAM_STATUSES = (ExamStatus.SCHEDULED, ExamStatus.IN_PROGRESS, ExamStatus.COMPLETED)


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
        .where(or_(
            Exam.teaching_assignment_id.is_(None),
            and_(Exam.status.in_(_LIVE_EXAM_STATUSES),
                 exists(select(ExamTarget.id).where(ExamTarget.exam_id == Exam.id, ExamTarget.student_id == Student.id))),
        ))
    )
    return list(session.execute(stmt).scalars().all())
