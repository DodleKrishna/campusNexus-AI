"""AgentOS V2 Phase 5: durable communication delivery (jobs, attempts, voice sessions, preferences, contact points).

A ``CommunicationJob`` is created exactly once per Guardian follow-up request (``idempotency_key`` and the
``(source_type, source_followup_id)`` unique index). It names its recipient by ``recipient_student_id`` only: no
job, attempt, session, event or audit row ever holds a phone number or e-mail address. Contact details live in
``contact_points`` (and the institution e-mail on ``users``) and are read only by
``app.communication.contacts.ContactResolver``, which only delivery connectors may call, immediately before a
delivery.

Nothing here stores audio, a call transcript, model reasoning or a provider credential. A ``VoiceSession`` keeps
only counters, a short safe goal and a structured outcome code.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UTCDateTime, portable_enum, utc_now


class CommunicationChannel(str, enum.Enum):
    IN_APP = "in_app"
    EMAIL = "email"
    VOICE = "voice"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class CommunicationJobStatus(str, enum.Enum):
    PENDING = "pending"  # created; waiting for the Communication Agent to choose a channel (or for approval)
    READY = "ready"  # a permitted channel was chosen; the delivery worker may attempt it
    IN_PROGRESS = "in_progress"  # an attempt is running (a voice call waits for its provider callback)
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"  # delivered and the recipient acknowledged / committed
    FAILED = "failed"  # attempts exhausted or a non-retryable refusal
    DEFERRED = "deferred"  # waiting for ``next_attempt_at`` (quiet hours, backoff after a failed attempt)
    CANCELLED = "cancelled"  # the source issue was resolved or closed before contact


ACTIVE_JOB_STATUSES = frozenset({CommunicationJobStatus.PENDING, CommunicationJobStatus.READY,
                                 CommunicationJobStatus.IN_PROGRESS, CommunicationJobStatus.DEFERRED})
DELIVERED_JOB_STATUSES = frozenset({CommunicationJobStatus.DELIVERED, CommunicationJobStatus.ACKNOWLEDGED})
TERMINAL_JOB_STATUSES = frozenset({CommunicationJobStatus.DELIVERED, CommunicationJobStatus.ACKNOWLEDGED,
                                   CommunicationJobStatus.FAILED, CommunicationJobStatus.CANCELLED})


class CommunicationAuthorization(str, enum.Enum):
    STANDING_POLICY = "standing_policy"  # routine reminder: the staff action that created the source + policy
    APPROVAL_REQUIRED = "approval_required"  # non-routine: a person must approve before any delivery
    APPROVED = "approved"
    REJECTED = "rejected"


class AttemptStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    ANSWERED = "answered"  # voice: the call was answered and the stream connected
    DELIVERED = "delivered"  # in-app / e-mail accepted; voice: completed after an answered call
    FAILED = "failed"


class VoiceSessionStatus(str, enum.Enum):
    PENDING = "pending"  # call placed; no stream yet
    CONNECTED = "connected"
    COMPLETED = "completed"
    FAILED = "failed"


class ContactKind(str, enum.Enum):
    EMAIL = "email"
    PHONE = "phone"


class CommunicationPreference(TenantMixin, Base):
    """A student's channel consent. Absent row = institution defaults (in-app and e-mail yes, voice/SMS/WhatsApp no)."""

    __tablename__ = "communication_preferences"
    __table_args__ = (Index("ux_communication_preferences_org_student", "organization_id", "student_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    allow_in_app: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_email: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_voice: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_whatsapp: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional personal quiet hours ("HH:MM", campus time). NULL = the institution's quiet hours.
    quiet_start: Mapped[Optional[str]] = mapped_column(String(5), default=None)
    quiet_end: Mapped[Optional[str]] = mapped_column(String(5), default=None)
    preferred_channel: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class ContactPoint(TenantMixin, Base):
    """A student's contact address for one kind. Read only by ``ContactResolver`` inside a delivery connector."""

    __tablename__ = "contact_points"
    __table_args__ = (Index("ux_contact_points_org_student_kind", "organization_id", "student_id", "kind", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    kind: Mapped[ContactKind] = mapped_column(portable_enum(ContactKind))
    address: Mapped[str] = mapped_column(String(320))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class CommunicationJob(TenantMixin, Base):
    __tablename__ = "communication_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_communication_jobs_idempotency_key"),
        # Exactly one job per source follow-up, enforced by the database.
        Index("ux_communication_jobs_source", "source_type", "source_followup_id", unique=True),
        # The delivery worker's scan: READY / DEFERRED jobs whose time has come.
        Index("ix_communication_jobs_org_status_next", "organization_id", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(20))  # assignment | exam | attendance | nexus
    source_id: Mapped[int] = mapped_column(Integer)  # the assignment / exam / intervention id
    source_followup_id: Mapped[int] = mapped_column(Integer)
    source_mission_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_missions.id"), default=None, index=True)
    source_event_id: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    agent_mission_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_missions.id"), default=None, index=True)
    recipient_student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(40))
    urgency: Mapped[str] = mapped_column(String(10))
    requested_channel: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    selected_channel: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    not_before: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    status: Mapped[CommunicationJobStatus] = mapped_column(portable_enum(CommunicationJobStatus),
                                                           default=CommunicationJobStatus.PENDING)
    authorization: Mapped[CommunicationAuthorization] = mapped_column(portable_enum(CommunicationAuthorization))
    authorized_by_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("auth_accounts.id"), default=None)
    authorized_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    outcome_code: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)


class CommunicationAttempt(TenantMixin, Base):
    __tablename__ = "communication_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_communication_attempts_job_attempt"),
        Index("ix_communication_attempts_provider_ref", "provider", "provider_reference"),
        Index("ix_communication_attempts_org_status", "organization_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("communication_jobs.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(30))
    provider_reference: Mapped[Optional[str]] = mapped_column(String(80), default=None)
    provider_status: Mapped[Optional[str]] = mapped_column(String(30), default=None)  # the mapped provider status
    status: Mapped[AttemptStatus] = mapped_column(portable_enum(AttemptStatus), default=AttemptStatus.IN_PROGRESS)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    answered_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    outcome_code: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class VoiceSession(TenantMixin, Base):
    __tablename__ = "voice_sessions"
    __table_args__ = (Index("ux_voice_sessions_attempt", "communication_attempt_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    communication_attempt_id: Mapped[int] = mapped_column(ForeignKey("communication_attempts.id"))
    provider_call_reference: Mapped[str] = mapped_column(String(80))
    stream_reference: Mapped[Optional[str]] = mapped_column(String(80), default=None)
    status: Mapped[VoiceSessionStatus] = mapped_column(portable_enum(VoiceSessionStatus),
                                                       default=VoiceSessionStatus.PENDING)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    max_turns: Mapped[int] = mapped_column(Integer)
    conversation_goal: Mapped[str] = mapped_column(String(200))  # a safe, fixed description of the purpose
    outcome_code: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    outcome: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)  # structured codes/counters only
    connected_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    ended_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
