"""Phase 16: faculty, teaching assignments and live attendance sessions.

``attendance_records`` (``app/db/models/academic.py``) keeps the per-course
attended/conducted counters the attendance rules read. The tables here hold
the per-class detail those counters are built from: one ``AttendanceSession``
per class meeting and at most one ``SessionAttendanceMark`` per student per
session (a unique constraint, never an application-level check alone).
Closing a session adds it to the counters exactly once
(``app/services/faculty_ops.py``).
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, utc_now


def _enum(cls: type[enum.Enum]) -> SAEnum:
    return SAEnum(cls, values_callable=lambda e: [m.value for m in e])


class FacultyProfile(Base):
    """A teaching staff member. ``auth_accounts.linked_faculty_id`` points here."""

    __tablename__ = "faculty_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), default=None)
    full_name: Mapped[str] = mapped_column(String(120))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    designation: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    department = relationship("Department")


class TeachingAssignment(Base):
    """Faculty X teaches course Y to section Z of a year/semester in an academic term.

    The roster is every student enrolled in the course for ``academic_term``
    whose ``students.section`` equals ``section``.
    """

    __tablename__ = "teaching_assignments"
    __table_args__ = (
        UniqueConstraint("course_id", "section", "academic_term", name="uq_teaching_assignments_course_section_term"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty_profiles.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    year: Mapped[int]
    semester: Mapped[int]
    section: Mapped[str] = mapped_column(String(10))
    academic_term: Mapped[str] = mapped_column(String(9))  # matches enrollments.academic_year

    faculty = relationship("FacultyProfile")
    course = relationship("Course")
    department = relationship("Department")


class AttendanceSessionStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class AttendanceSession(Base):
    """One meeting of a taught class.

    A recurring meeting has ``timetable_slot_id`` set; an extra class does
    not. ``(teaching_assignment_id, scheduled_start)`` is unique, so the same
    meeting can never exist twice.
    """

    __tablename__ = "attendance_sessions"
    __table_args__ = (
        UniqueConstraint("teaching_assignment_id", "scheduled_start", name="uq_attendance_sessions_assignment_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    teaching_assignment_id: Mapped[int] = mapped_column(ForeignKey("teaching_assignments.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty_profiles.id"), index=True)
    timetable_slot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("timetable_slots.id"), default=None)
    session_date: Mapped[date] = mapped_column(index=True)  # campus-local date
    scheduled_start: Mapped[datetime] = mapped_column(UTCDateTime)
    scheduled_end: Mapped[datetime] = mapped_column(UTCDateTime)
    room: Mapped[str] = mapped_column(String(80))
    status: Mapped[AttendanceSessionStatus] = mapped_column(
        _enum(AttendanceSessionStatus), default=AttendanceSessionStatus.SCHEDULED
    )
    actual_started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    actual_closed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    note: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    teaching_assignment = relationship("TeachingAssignment")
    course = relationship("Course")
    faculty = relationship("FacultyProfile")


class AttendanceMarkStatus(str, enum.Enum):
    PRESENT = "present"
    ABSENT = "absent"
    LATE = "late"
    EXCUSED = "excused"


class SessionAttendanceMark(Base):
    """One student's attendance in one session. Changing a mark updates this row."""

    __tablename__ = "session_attendance_marks"
    __table_args__ = (UniqueConstraint("session_id", "student_id", name="uq_session_attendance_marks_session_student"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("attendance_sessions.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    status: Mapped[AttendanceMarkStatus] = mapped_column(_enum(AttendanceMarkStatus))
    marked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    marked_by_faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty_profiles.id"))

    session = relationship("AttendanceSession")
    student = relationship("Student")
