"""AgentOS V2 Phase 3: assignments, their published target snapshot, submissions and follow-up requests.

An ``Assignment`` belongs to one teaching assignment (course + section + term). Publishing
snapshots the roster into ``assignment_targets`` once; later roster changes never alter
who an assignment targets. ``assignment_submissions`` holds at most one submission per
target. ``assignment_followups`` records a communication *request* raised by the
Assignment Guardian after deterministic policy allowed it -- nothing is sent in Phase 3,
and no row here ever holds a phone number or e-mail address.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UTCDateTime, portable_enum, utc_now


class AssignmentStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class SubmissionStatus(str, enum.Enum):
    SUBMITTED = "submitted"  # at or before the deadline
    LATE = "late"


class FollowupStatus(str, enum.Enum):
    REQUESTED = "requested"  # active: waiting for the Communication Agent (Phase 5)
    CANCELLED = "cancelled"  # closed without contact (student submitted, assignment cancelled)
    # Phase 5: closed by the communication system once its job ended. Only DELIVERED is a successful contact.
    DELIVERED = "delivered"
    FAILED = "failed"  # every attempt failed; never counted as contact


class Assignment(TenantMixin, Base):
    __tablename__ = "assignments"
    __table_args__ = (Index("ix_assignments_org_status_deadline", "organization_id", "status", "deadline_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_account_id: Mapped[int] = mapped_column(ForeignKey("auth_accounts.id"), index=True)
    created_by_faculty_id: Mapped[Optional[int]] = mapped_column(ForeignKey("faculty_profiles.id"), default=None)
    teaching_assignment_id: Mapped[int] = mapped_column(ForeignKey("teaching_assignments.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[AssignmentStatus] = mapped_column(portable_enum(AssignmentStatus), default=AssignmentStatus.DRAFT)
    published_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    deadline_at: Mapped[datetime] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    # Set once, in the publishing transaction: the one Assignment Guardian mission for this assignment.
    guardian_mission_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_missions.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class AssignmentTarget(TenantMixin, Base):
    """One targeted student, snapshotted at publication."""

    __tablename__ = "assignment_targets"
    __table_args__ = (
        UniqueConstraint("assignment_id", "student_id", name="uq_assignment_targets_assignment_student"),
        Index("ix_assignment_targets_org_student", "organization_id", "student_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    student_code: Mapped[str] = mapped_column(String(20))  # snapshot
    section: Mapped[Optional[str]] = mapped_column(String(10), default=None)  # snapshot
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class AssignmentSubmission(TenantMixin, Base):
    __tablename__ = "assignment_submissions"
    __table_args__ = (UniqueConstraint("assignment_id", "student_id", name="uq_assignment_submissions_assignment_student"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    submitted_by_account_id: Mapped[int] = mapped_column(ForeignKey("auth_accounts.id"))
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[SubmissionStatus] = mapped_column(portable_enum(SubmissionStatus))
    # Optional short text answer. Never copied into events, audit, tool results or an AgentBrain context.
    content_text: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


_ACTIVE_FOLLOWUP = f"status = '{FollowupStatus.REQUESTED.value}'"


class AssignmentFollowup(TenantMixin, Base):
    """A policy-approved request to contact one student (no contact details; delivery is Phase 5)."""

    __tablename__ = "assignment_followups"
    __table_args__ = (
        Index("ix_assignment_followups_assignment_student_status", "assignment_id", "student_id", "status"),
        UniqueConstraint("assignment_id", "student_id", "attempt_number", name="uq_assignment_followups_attempt"),
        # At most one active request per student and assignment, enforced by the database.
        Index("ux_assignment_followups_active", "assignment_id", "student_id", unique=True,
              sqlite_where=text(_ACTIVE_FOLLOWUP), postgresql_where=text(_ACTIVE_FOLLOWUP)),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"))
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
