"""Read-only academic repository functions."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.academic import AttendanceRecord, Enrollment, Exam, TimetableSlot
from app.db.models.identity import Student


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
