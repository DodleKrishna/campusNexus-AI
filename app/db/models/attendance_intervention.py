"""AgentOS V2 Phase 4: attendance monitoring on top of the Phase 16 class sessions.

Attendance itself stays where it is (``attendance_sessions`` + ``session_attendance_marks``, written only by
the faculty operations). An ``AttendanceIntervention`` is one confirmed, unexplained absence of one student
from one held class session, monitored by one Attendance Guardian mission. ``attendance_followups`` records a
policy-approved communication *request* for it (no contact details; delivery is Phase 5).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UTCDateTime, portable_enum, utc_now
from app.db.models.assignment import FollowupStatus


class InterventionStatus(str, enum.Enum):
    OPEN = "open"  # active: the Guardian is monitoring it
    RESOLVED = "resolved"  # resolution_code says how (corrected / justified / leave / recorded)
    UNRESOLVED = "unresolved"  # the resolution window closed without a resolution
    CANCELLED = "cancelled"  # the class was cancelled or the session is gone


_OPEN = f"status = '{InterventionStatus.OPEN.value}'"


class AttendanceIntervention(TenantMixin, Base):
    __tablename__ = "attendance_interventions"
    __table_args__ = (
        # At most one active intervention per student and class session, enforced by the database.
        Index("ux_attendance_interventions_active", "attendance_session_id", "student_id", unique=True,
              sqlite_where=text(_OPEN), postgresql_where=text(_OPEN)),
        Index("ix_attendance_interventions_org_status", "organization_id", "status"),
        Index("ix_attendance_interventions_org_student", "organization_id", "student_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    attendance_session_id: Mapped[int] = mapped_column(ForeignKey("attendance_sessions.id"), index=True)
    teaching_assignment_id: Mapped[int] = mapped_column(ForeignKey("teaching_assignments.id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    mission_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_missions.id"), index=True, default=None)
    status: Mapped[InterventionStatus] = mapped_column(portable_enum(InterventionStatus), default=InterventionStatus.OPEN)
    detected_mark: Mapped[str] = mapped_column(String(20))  # "absent" (marked) or "unmarked" at detection
    resolution_code: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    # A staff member's explicit approval of the student's justification (an allowlisted code, never free text).
    justification_code: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    justified_by_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("auth_accounts.id"), default=None)
    justified_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class AttendanceFollowup(TenantMixin, Base):
    __tablename__ = "attendance_followups"
    __table_args__ = (
        UniqueConstraint("intervention_id", "attempt_number", name="uq_attendance_followups_attempt"),
        Index("ux_attendance_followups_active", "intervention_id", unique=True,
              sqlite_where=text(f"status = '{FollowupStatus.REQUESTED.value}'"),
              postgresql_where=text(f"status = '{FollowupStatus.REQUESTED.value}'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    intervention_id: Mapped[int] = mapped_column(ForeignKey("attendance_interventions.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    mission_id: Mapped[int] = mapped_column(ForeignKey("agent_missions.id"), index=True)
    channel_requested: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    purpose: Mapped[str] = mapped_column(String(40))
    urgency: Mapped[str] = mapped_column(String(10))
    status: Mapped[FollowupStatus] = mapped_column(portable_enum(FollowupStatus), default=FollowupStatus.REQUESTED)
    attempt_number: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))
    not_before_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
