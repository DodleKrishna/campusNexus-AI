"""Phase 16: institutional workflow requests (permission, leave, OD).

Deliberately separate from ``approval_records``: those authorize one exact
Action Agent tool call inside a mission. A ``WorkflowRequest`` is a student's
request to a person (their faculty or mentor), decided by that person.

``operation_audit_events`` is the append-only audit trail for these
operations and for attendance writes, which have no mission to hang a
``audit_logs`` row on.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import JSON, Enum as SAEnum
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, utc_now


class WorkflowRequestType(str, enum.Enum):
    EVENT_PERMISSION = "event_permission"
    ATTENDANCE_PERMISSION = "attendance_permission"
    LEAVE_REQUEST = "leave_request"
    OD_REQUEST = "od_request"


class WorkflowRequestStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING = "pending"
    # Submitted, but deterministic routing found no reviewer. Nobody can decide
    # it until an administrator routes it (a later phase).
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class WorkflowRequest(Base):
    __tablename__ = "workflow_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    request_type: Mapped[WorkflowRequestType] = mapped_column(
        SAEnum(WorkflowRequestType, values_callable=lambda e: [m.value for m in e])
    )
    status: Mapped[WorkflowRequestStatus] = mapped_column(
        SAEnum(WorkflowRequestStatus, values_callable=lambda e: [m.value for m in e]),
        default=WorkflowRequestStatus.DRAFT,
    )
    requester_account_id: Mapped[int] = mapped_column(ForeignKey("auth_accounts.id"))
    requester_role: Mapped[str] = mapped_column(String(20))
    student_id: Mapped[Optional[str]] = mapped_column(ForeignKey("students.student_code"), index=True, default=None)
    reviewer_faculty_id: Mapped[Optional[int]] = mapped_column(ForeignKey("faculty_profiles.id"), index=True, default=None)
    department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id"), default=None)
    title: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    # Structured, server-collected context (event, affected classes, attendance...).
    context: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    # How the reviewer was chosen: affected_course_faculty / mentor / unresolved.
    routing_basis: Mapped[str] = mapped_column(String(40))
    routing_note: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    decided_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    decided_by_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("auth_accounts.id"), default=None)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, default=None)

    reviewer = relationship("FacultyProfile")


class OperationAuditEvent(Base):
    """Append-only: who did what to which campus-operations record, and when."""

    __tablename__ = "operation_audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    actor_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("auth_accounts.id"), default=None)
    actor_role: Mapped[str] = mapped_column(String(20))
    subject_type: Mapped[str] = mapped_column(String(40))  # attendance_session / workflow_request
    subject_id: Mapped[str] = mapped_column(String(40), index=True)
    message: Mapped[str] = mapped_column(Text)
    event_metadata: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
