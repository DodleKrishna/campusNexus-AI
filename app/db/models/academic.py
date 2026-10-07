"""Academic domain: courses, enrollments, attendance, timetable, exams.

Attendance is stored as raw ``classes_attended`` / ``classes_conducted``
counts, never a pre-computed percentage -- CLAUDE.md requires that any
official-rule calculation (e.g. an attendance-shortage check) be plain Python
over source data, not a cached derived value.
"""
from __future__ import annotations

import enum
from datetime import datetime, time
from typing import Optional

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, portable_enum, UTCDateTime, utc_now, TenantMixin


class CourseStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Course(TenantMixin, Base):
    """A course offering (one row per course, not per section)."""

    __tablename__ = "courses"
    __table_args__ = (Index("ux_courses_org_code", "organization_id", "code", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), index=True)  # unique per organization (Phase 22B)
    title: Mapped[str] = mapped_column(String(150))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    credits: Mapped[int]
    semester: Mapped[int]
    instructor: Mapped[str] = mapped_column(String(120))
    status: Mapped[CourseStatus] = mapped_column(
        portable_enum(CourseStatus),
        default=CourseStatus.ACTIVE,
    )

    department = relationship("Department")


class Enrollment(TenantMixin, Base):
    """A student's enrollment in a course for a given academic year."""

    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("student_id", "course_id", "academic_year", name="uq_enrollments_student_course_year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    academic_year: Mapped[str] = mapped_column(String(9))  # e.g. "2025-2026"
    semester: Mapped[int]
    grade: Mapped[Optional[str]] = mapped_column(String(4), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    student = relationship("Student")
    course = relationship("Course")
    attendance: Mapped[Optional["AttendanceRecord"]] = relationship(
        back_populates="enrollment", uselist=False, cascade="all, delete-orphan"
    )


class AttendanceRecord(TenantMixin, Base):
    """Raw attendance counters for one enrollment.

    Percentage is deliberately NOT stored here; future deterministic rule
    modules (app/rules/) compute it from ``classes_attended`` / ``classes_conducted``.
    """

    __tablename__ = "attendance_records"
    __table_args__ = (
        CheckConstraint("classes_attended >= 0", name="classes_attended_non_negative"),
        CheckConstraint("classes_conducted >= 0", name="classes_conducted_non_negative"),
        CheckConstraint("classes_attended <= classes_conducted", name="attended_not_exceeding_conducted"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), unique=True)
    classes_attended: Mapped[int]
    classes_conducted: Mapped[int]
    last_updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    enrollment: Mapped["Enrollment"] = relationship(back_populates="attendance")


class TimetableSlot(TenantMixin, Base):
    """A recurring weekly class slot for a course.

    ``weekday`` is 0=Monday .. 6=Sunday. Start/end are wall-clock local times
    for a recurring slot (no calendar date), so timezone conversion does not
    apply the way it does for absolute timestamps like ``Exam.scheduled_start``.
    """

    __tablename__ = "timetable_slots"
    __table_args__ = (CheckConstraint("weekday >= 0 AND weekday <= 6", name="weekday_range"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    weekday: Mapped[int]
    start_time: Mapped[time] = mapped_column()
    end_time: Mapped[time] = mapped_column()
    location: Mapped[str] = mapped_column(String(80))
    semester: Mapped[int]

    course = relationship("Course")


class ExamStatus(str, enum.Enum):
    """Lifecycle of a Guardian-managed exam (AgentOS V2 Phase 4). NULL on a timetable-only exam row."""

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ExamType(str, enum.Enum):
    MID = "mid"
    INTERNAL = "internal"
    QUIZ = "quiz"
    OTHER = "other"


class Exam(TenantMixin, Base):
    """A scheduled exam for a course.

    Phase 4 (additive, nullable): a *managed* exam also has a class (``teaching_assignment_id``), a
    creator, a title, a duration, a lifecycle ``status`` and its Exam Guardian mission. Rows without a
    ``teaching_assignment_id`` are the original timetable-only exams and behave exactly as before.
    ``scheduled_start`` is the exam's ``scheduled_at``; ``scheduled_end`` = start + duration.
    """

    __tablename__ = "exams"
    __table_args__ = (Index("ix_exams_org_status_scheduled", "organization_id", "status", "scheduled_start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    exam_type: Mapped[str] = mapped_column(String(30))  # "midterm" | "final" | "quiz"; managed: an ExamType value
    scheduled_start: Mapped[datetime] = mapped_column(UTCDateTime)
    scheduled_end: Mapped[datetime] = mapped_column(UTCDateTime)
    location: Mapped[str] = mapped_column(String(80))
    # --- Phase 4: managed exams (all nullable so ``upgrade_schema`` can add them) ---
    teaching_assignment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teaching_assignments.id"), index=True,
                                                                  default=None)
    created_by_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("auth_accounts.id"), default=None)
    title: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    duration_minutes: Mapped[Optional[int]] = mapped_column(default=None)
    status: Mapped[Optional[ExamStatus]] = mapped_column(portable_enum(ExamStatus), default=None)
    makeup_deadline_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    # Set once, in the scheduling transaction: the one Exam Guardian mission for this exam.
    guardian_mission_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_missions.id"), default=None)
    published_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)  # when it was scheduled (announced)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)

    course = relationship("Course")
