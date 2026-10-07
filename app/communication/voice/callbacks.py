"""Exotel call-status callbacks (AgentOS V2 Phase 5).

The provider's request is unauthenticated, so the signed callback token (``tokens``) is verified first; it names the
organization and the attempt, the tenant session is opened for that organization only, and the attempt must still
carry the provider's ``CallSid``. Statuses are mapped by ``communication_policy.map_exotel_status``:

* not terminal -- recorded (``ANSWERED`` once answered);
* completed after an answer -- the attempt is DELIVERED; the job is DELIVERED (ACKNOWLEDGED when the conversation's
  outcome was an acknowledgement) and the follow-up closed, all through ``service``;
* busy / no-answer / failed / cancelled -- the attempt FAILED; retried with backoff or the job fails
  (``worker.record_failed_attempt``).

Idempotent: a callback for an attempt that is already final changes nothing. Nothing from the request other than the
call reference, the mapped status and a duration is kept.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agentos.schemas import DomainEventType
from app.communication import service
from app.communication.voice import tokens
from app.communication.worker import record_failed_attempt
from app.db.models.communication_delivery import (
    AttemptStatus, CommunicationAttempt, CommunicationJob, CommunicationJobStatus, VoiceSession, VoiceSessionStatus,
)
from app.db.tenant_session import TenantSessionFactory
from app.rules import communication_policy as policy_rules

DELIVERED_VOICE = "DELIVERED_VOICE"
FINAL_ATTEMPTS = frozenset({AttemptStatus.DELIVERED, AttemptStatus.FAILED})


class StatusCallback(BaseModel):
    token: str = Field(max_length=400)
    call_sid: str = Field(max_length=80)
    status: str = Field(max_length=30)
    duration_seconds: Optional[int] = Field(default=None, ge=0, le=86400)


class CallbackResult(BaseModel):
    result: Literal["updated", "ignored", "rejected"]
    code: Optional[str] = None


def handle_status_callback(factory: Any, setup: Any, callback: StatusCallback, now: datetime) -> CallbackResult:
    """``setup``: the runtime's ``CommunicationSetup`` (adapters + policy)."""
    try:
        claims = tokens.verify(callback.token, "callback", now)
    except tokens.VoiceTokenError as exc:
        return CallbackResult(result="rejected", code=f"TOKEN_{exc}"[:64])
    factory = TenantSessionFactory.from_sessionmaker(factory)
    with factory.open_tenant_session(claims.organization_id) as session:
        attempt = session.get(CommunicationAttempt, claims.attempt_id)  # tenant-filtered
        if attempt is None or attempt.provider_reference != callback.call_sid:
            return CallbackResult(result="rejected", code="ATTEMPT_NOT_FOUND")
        if attempt.status in FINAL_ATTEMPTS:
            return CallbackResult(result="ignored", code="ALREADY_FINAL")
        job = session.get(CommunicationJob, attempt.job_id)
        voice = session.execute(select(VoiceSession).where(
            VoiceSession.communication_attempt_id == attempt.id)).scalars().first()
        mapped = policy_rules.map_exotel_status(callback.status)
        attempt.provider_status, attempt.updated_at = mapped.internal, now
        if mapped.answered and attempt.answered_at is None:
            attempt.status, attempt.answered_at = AttemptStatus.ANSWERED, now
        if not mapped.terminal:
            session.commit()
            return CallbackResult(result="updated", code=mapped.internal)

        attempt.completed_at, attempt.duration_seconds = now, callback.duration_seconds
        if voice is not None:
            voice.ended_at = voice.ended_at or now
        if mapped.error_code is None and attempt.answered_at is not None:
            settle_answered_call(session, setup, job, attempt, voice, now)
            code = job.status.value
        else:
            attempt.status, attempt.error_code = AttemptStatus.FAILED, mapped.error_code or "CALL_FAILED"
            if voice is not None:
                voice.status = VoiceSessionStatus.FAILED
            if job.status == CommunicationJobStatus.IN_PROGRESS:
                record_failed_attempt(session, job, attempt.error_code, now, setup.adapters, setup.policy)
            code = attempt.error_code
        session.commit()
        return CallbackResult(result="updated", code=code)


def settle_answered_call(session: Any, setup: Any, job: CommunicationJob, attempt: CommunicationAttempt,
                         voice: Optional[VoiceSession], now: datetime) -> None:
    """An answered call has completed: the attempt is delivered, then the job (once), with the conversation's
    outcome (if the stream already reported one)."""
    outcome = voice.outcome_code if voice is not None else None
    attempt.status, attempt.outcome_code = AttemptStatus.DELIVERED, outcome or DELIVERED_VOICE
    if voice is not None and voice.status != VoiceSessionStatus.FAILED:
        voice.status = VoiceSessionStatus.COMPLETED
    if job.status == CommunicationJobStatus.IN_PROGRESS:
        service.mark_delivered(session, setup.adapters, job, DELIVERED_VOICE, now)
    apply_outcome(session, job, outcome, now)


def apply_outcome(session: Any, job: CommunicationJob, outcome: Optional[str], now: datetime) -> None:
    """A recipient's structured response. An acknowledgement upgrades a delivered job to ACKNOWLEDGED; a response
    that needs a person (help, dispute, callback) is reported to the Guardian and audited for staff review. A
    response never changes an attendance, submission or exam record."""
    if outcome is None or job.status not in (CommunicationJobStatus.DELIVERED, CommunicationJobStatus.ACKNOWLEDGED):
        return
    if policy_rules.is_acknowledgement(outcome) and job.status == CommunicationJobStatus.DELIVERED:
        job.status, job.updated_at = CommunicationJobStatus.ACKNOWLEDGED, now
        service.audit(session, job, "COMMUNICATION_ACKNOWLEDGED", f"Communication job {job.id} acknowledged.", now,
                      outcome_code=outcome)
        service.notify(session, job, DomainEventType.COMMUNICATION_ACKNOWLEDGED, now, outcome_code=outcome)
    elif policy_rules.outcome_requires_review(outcome):
        service.audit(session, job, "COMMUNICATION_RESPONSE_NEEDS_REVIEW",
                      f"Communication job {job.id}: the recipient's response needs staff review.", now,
                      outcome_code=outcome)
        service.notify(session, job, DomainEventType.COMMUNICATION_RESPONSE_RECEIVED, now, outcome_code=outcome,
                       needs_review=True)
