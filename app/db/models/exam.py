"""AgentOS V2 Phase 4: a managed exam's target snapshot, attendance and follow-up requests.

The exam itself is the existing ``exams`` row (``app.db.models.academic.Exam``), extended additively.
Scheduling snapshots the class roster into ``exam_targets`` once. ``exam_attendance`` holds at most one
row per (exam, student), written only by the exam service from an authorized staff member's marking.
``exam_followups`` records a communication *request* raised by the Exam Guardian after deterministic policy
allowed it -- nothing is sent in Phase 4, and no row here ever holds a phone number or e-mail address.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UTCDateTime, portable_enum, utc_now
from app.db.models.assignment import FollowupStatus


class ExamAttendanceStatus(str, enum.Enum):
    PRESENT = "present"
    ABSENT = "absent"
    EXEMPT = "exempt"
    MAKEUP_COMPLETED = "makeup_completed"


class ExamTarget(TenantMixin, Base):
    """One targeted student, snapshotted when the exam was scheduled."""

    __tablename__ = "exam_targets"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_id", name="uq_exam_targets_exam_student"),
        Index("ix_exam_targets_org_student", "organization_id", "student_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    student_code: Mapped[str] = mapped_column(String(20))  # snapshot
    section: Mapped[Optional[str]] = mapped_column(String(10), default=None)  # snapshot
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class ExamAttendance(TenantMixin, Base):
    __tablename__ = "exam_attendance"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_id", name="uq_exam_attendance_exam_student"),
        Index("ix_exam_attendance_exam_status", "exam_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    status: Mapped[ExamAttendanceStatus] = mapped_column(portable_enum(ExamAttendanceStatus))
    marked_at: Mapped[datetime] = mapped_column(UTCDateTime)
    marked_by_account_id: Mapped[int] = mapped_column(ForeignKey("auth_accounts.id"))
    reason_code: Mapped[Optional[str]] = mapped_column(String(32), default=None)  # an allowlisted code, never free text
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


_ACTIVE_FOLLOWUP = f"status = '{FollowupStatus.REQUESTED.value}'"


class ExamFollowup(TenantMixin, Base):
    """A policy-approved request to contact one student about one exam (no contact details; delivery is Phase 5).

    ``purpose`` is ``exam_reminder`` (before the exam) or ``absence_followup`` (confirmed absent). Attempts,
    cooldown and active-request suppression are counted per purpose.
    """

    __tablename__ = "exam_followups"
    __table_args__ = (
        Index("ix_exam_followups_exam_student_status", "exam_id", "student_id", "status"),
        UniqueConstraint("exam_id", "student_id", "purpose", "attempt_number", name="uq_exam_followups_attempt"),
        # At most one active request per student, exam and purpose, enforced by the database.
        Index("ux_exam_followups_active", "exam_id", "student_id", "purpose", unique=True,
              sqlite_where=text(_ACTIVE_FOLLOWUP), postgresql_where=text(_ACTIVE_FOLLOWUP)),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    mission_id: Mapped[int] = mapped_column(ForeignKey("agent_missions.id"), index=True)
    channel_requested: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    purpose: Mapped[str] = mapped_column(String(40))
    urgency: Mapped[str] = mapped_column(String(10))
    status: Mapped[FollowupStatus] = mapped_column(portable_enum(FollowupStatus), default=FollowupStatus.REQUESTED)
    attempt_number: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))
    not_before_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)  # quiet hours
    # Phase 5 (nullable, additive): the communication job that handled this request and its structured result.
    communication_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("communication_jobs.id"), default=None)
    outcome_code: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
