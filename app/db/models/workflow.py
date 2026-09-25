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
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, portable_enum, UTCDateTime, utc_now, TenantMixin


class WorkflowRequestType(str, enum.Enum):
    EVENT_PERMISSION = "event_permission"
    ATTENDANCE_PERMISSION = "attendance_permission"
    LEAVE_REQUEST = "leave_request"
    OD_REQUEST = "od_request"
    # Phase 17: faculty-originated requests, routed to the department HOD.
    FACULTY_LEAVE = "faculty_leave"
    CLASS_SUBSTITUTION = "class_substitution"
    DEPARTMENT_PERMISSION = "department_permission"
    # Phase 18: head-of-department requests, routed to the administration.
    HOD_LEAVE = "hod_leave"
    DEPARTMENT_RESOURCE = "department_resource"
    ADMIN_ESCALATION = "admin_escalation"


class WorkflowRequestStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING = "pending"
    # Submitted, but deterministic routing found no reviewer. Nobody can decide
    # it until an administrator routes it (a later phase).
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class WorkflowRequest(TenantMixin, Base):
    __tablename__ = "workflow_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    request_type: Mapped[WorkflowRequestType] = mapped_column(
        portable_enum(WorkflowRequestType)
    )
    status: Mapped[WorkflowRequestStatus] = mapped_column(
        portable_enum(WorkflowRequestStatus),
        default=WorkflowRequestStatus.DRAFT,
    )
    requester_account_id: Mapped[int] = mapped_column(ForeignKey("auth_accounts.id"))
    requester_role: Mapped[str] = mapped_column(String(20))
    # Phase 17 (nullable, additive): set when a faculty member (or HOD) is the requester.
    requester_faculty_id: Mapped[Optional[int]] = mapped_column(ForeignKey("faculty_profiles.id"), index=True, default=None)
    student_id: Mapped[Optional[str]] = mapped_column(ForeignKey("students.student_code"), index=True, default=None)
    reviewer_faculty_id: Mapped[Optional[int]] = mapped_column(ForeignKey("faculty_profiles.id"), index=True, default=None)
    # Phase 18 (nullable, additive): "admin" when the request is routed to the administration
    # (any active admin account may decide it); null for a faculty/HOD reviewer.
    reviewer_role: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    # Phase 18: every routing step, oldest first: {at, basis, reviewer, note}.
    routing_history: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, default=None)
    department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id"), default=None)
    title: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    # Structured, server-collected context (event, affected classes, attendance...).
    context: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    # How the reviewer was chosen: affected_course_faculty / mentor / hod_escalation /
    # department_hod / unresolved.
    routing_basis: Mapped[str] = mapped_column(String(40))
    routing_note: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    decided_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    decided_by_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("auth_accounts.id"), default=None)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, default=None)

    reviewer = relationship("FacultyProfile", foreign_keys=[reviewer_faculty_id])
    requester_faculty = relationship("FacultyProfile", foreign_keys=[requester_faculty_id])


class OperationAuditEvent(TenantMixin, Base):
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
