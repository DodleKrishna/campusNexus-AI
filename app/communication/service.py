"""Communication jobs (AgentOS V2 Phase 5): intake of COMMUNICATION_REQUESTED and every job transition.

``consume_requests`` turns each pending COMMUNICATION_REQUESTED event (a Guardian follow-up) into exactly one
``CommunicationJob``. The event payload is never trusted: the follow-up is reloaded through its ``SourceAdapter``
in the caller's tenant session, so a follow-up of another organization, a missing one or one whose issue is already
resolved is rejected (and an active-but-moot follow-up is closed CANCELLED, so its Guardian is not blocked). A
repeated event, or a second worker racing the first, finds the existing job (``idempotency_key`` plus the
``(source_type, source_followup_id)`` unique index) and creates nothing.

Each new job gets one Communication Agent mission, owned by the Guardian mission's owner (the staff member whose
action created the source: standing approval for routine reminders). Contact details are never read here.
All functions stage writes in the caller's transaction; the caller commits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agentos import policy_env
from app.agentos.events import SUBJECT_MISSION, EventService, wake_mission
from app.agentos.schemas import CreateAgentMission, DomainEventType
from app.agentos.worker import _owner_actor as owner_actor
from app.communication.sources import FOLLOWUP_SUBJECTS, SourceAdapter, SourceUnavailable, adapter_for
from app.db.models.agent_kernel import AgentMission, DomainEvent
from app.db.models.assignment import FollowupStatus
from app.db.models.communication_delivery import (
    ACTIVE_JOB_STATUSES, CommunicationAuthorization, CommunicationJob, CommunicationJobStatus, CommunicationPreference,
)
from app.db.repositories import operations_audit
from app.rules import communication_policy as policy_rules
from app.rules.followup_policy import FollowupPolicy
from app.services.class_schedule import CAMPUS_TZ

SUBJECT = "communication_job"
ACTOR_ROLE = "communication"
COMMUNICATION_AGENT_KEY = "communication_agent"
AGENT_MAX_STEPS = 12
INTAKE_BATCH = 50
ENV_MAX_ATTEMPTS = "CAMPUSNEXUS_COMMUNICATION_MAX_ATTEMPTS"  # 3
ENV_BACKOFF_MINUTES = "CAMPUSNEXUS_COMMUNICATION_BACKOFF_MINUTES"  # 30
ENV_FALLBACK = "CAMPUSNEXUS_COMMUNICATION_ALLOW_FALLBACK"  # "1" allows a channel switch after a failure
ENV_VOICE_TIME_LIMIT = "CAMPUSNEXUS_VOICE_TIME_LIMIT_SECONDS"  # 180 (30-600)
DEMO_VOICE_TIME_LIMIT = 60  # CAMPUSNEXUS_DEMO_MODE=1 caps every call at one minute

_events = EventService()


def policy_from_env() -> policy_rules.CommunicationPolicy:
    """Defaults: 3 attempts per job, 30 min backoff (doubling, capped at 6 h), no channel fallback, the institution's
    follow-up quiet hours in campus time. An invalid value raises ``ValueError`` at startup."""
    quiet_start, quiet_end = policy_env.quiet_hours()
    voice_limit = int(policy_env.number(ENV_VOICE_TIME_LIMIT, 180))
    if (os.environ.get("CAMPUSNEXUS_DEMO_MODE") or "").strip() == "1":
        voice_limit = min(voice_limit, DEMO_VOICE_TIME_LIMIT)
    return policy_rules.CommunicationPolicy(
        voice_time_limit_seconds=voice_limit,
        max_attempts=int(policy_env.number(ENV_MAX_ATTEMPTS, 3)),
        backoff=timedelta(minutes=policy_env.number(ENV_BACKOFF_MINUTES, 30)),
        allow_channel_fallback=(os.environ.get(ENV_FALLBACK) or "").strip() == "1",
        contact=FollowupPolicy(quiet_start=quiet_start, quiet_end=quiet_end, timezone=CAMPUS_TZ))


def audit(session: Session, job: CommunicationJob, event_type: str, message: str, now: datetime, **metadata: Any) -> None:
    operations_audit.record(session, event_type=event_type, actor_account_id=None, actor_role=ACTOR_ROLE,
                            subject_type=SUBJECT, subject_id=str(job.id), message=message, at=now,
                            metadata={"source_type": job.source_type, "source_followup_id": job.source_followup_id,
                                      "status": job.status.value, **metadata})


def consent_for(session: Session, student_id: int) -> policy_rules.Consent:
    row = session.execute(select(CommunicationPreference).where(
        CommunicationPreference.student_id == student_id)).scalars().first()
    if row is None:
        return policy_rules.Consent()
    return policy_rules.Consent(in_app=row.allow_in_app, email=row.allow_email, voice=row.allow_voice,
                                sms=row.allow_sms, whatsapp=row.allow_whatsapp, preferred=row.preferred_channel,
                                quiet_start=policy_rules.parse_hhmm(row.quiet_start),
                                quiet_end=policy_rules.parse_hhmm(row.quiet_end))


def job_for_followup(session: Session, source_type: str, followup_id: int) -> Optional[CommunicationJob]:
    return session.execute(select(CommunicationJob).where(
        CommunicationJob.source_type == source_type, CommunicationJob.source_followup_id == followup_id)).scalars().first()


def idempotency_key(source_type: str, followup_id: int) -> str:
    return f"followup:{source_type}:{followup_id}"


# --- Intake ------------------------------------------------------------------------------------------------------------


class IntakeResult(BaseModel):
    event_id: int
    result: Literal["created", "duplicate", "rejected"]
    job_id: Optional[int] = None
    code: Optional[str] = None


def consume_requests(session: Session, runtime: Any, adapters: Dict[str, SourceAdapter],
                     policy: policy_rules.CommunicationPolicy, now: datetime, *, organization_id: int,
                     limit: int = INTAKE_BATCH) -> List[IntakeResult]:
    """Consume up to ``limit`` pending COMMUNICATION_REQUESTED events of this tenant (oldest first)."""
    events = list(session.execute(select(DomainEvent).where(
        DomainEvent.consumed_at.is_(None), DomainEvent.event_type == DomainEventType.COMMUNICATION_REQUESTED.value,
        DomainEvent.subject_type.in_(list(FOLLOWUP_SUBJECTS))).order_by(DomainEvent.id).limit(limit)).scalars())
    return [_intake(session, runtime, adapters, policy, event, now, organization_id) for event in events]


def _reject(session: Session, event: DomainEvent, code: str, now: datetime) -> IntakeResult:
    operations_audit.record(session, event_type="COMMUNICATION_REQUEST_REJECTED", actor_account_id=None,
                            actor_role=ACTOR_ROLE, subject_type="domain_event", subject_id=str(event.id), at=now,
                            message=f"Communication request {event.id} rejected ({code}).",
                            metadata={"subject_type": event.subject_type, "code": code})
    return IntakeResult(event_id=event.id, result="rejected", code=code)


def _intake(session: Session, runtime: Any, adapters: Dict[str, SourceAdapter], policy: policy_rules.CommunicationPolicy,
            event: DomainEvent, now: datetime, organization_id: int) -> IntakeResult:
    event.consumed_at = now
    source_type = FOLLOWUP_SUBJECTS[event.subject_type]
    try:
        followup_id = int(event.subject_id or "")
    except ValueError:
        return _reject(session, event, "INVALID_SUBJECT", now)
    existing = job_for_followup(session, source_type, followup_id)
    if existing is not None:
        return IntakeResult(event_id=event.id, result="duplicate", job_id=existing.id)
    adapter = adapter_for(adapters, source_type)
    try:
        followup = adapter.load(session, followup_id)
        closed = adapter.closed_reason(session, followup, now)
        adapter.facts(session, followup)  # every linked record a message needs must exist
    except SourceUnavailable as exc:
        return _reject(session, event, exc.code, now)
    if closed is not None:
        adapter.close(followup, FollowupStatus.CANCELLED, job_id=None, outcome_code=closed, now=now)
        return _reject(session, event, closed, now)

    authorization = CommunicationAuthorization(policy_rules.authorization_for(source_type, followup.purpose))
    job = CommunicationJob(
        source_type=source_type, source_id=adapter.source_id(followup), source_followup_id=followup.id,
        source_mission_id=followup.mission_id, source_event_id=event.id, recipient_student_id=followup.student_id,
        purpose=followup.purpose, urgency=followup.urgency,
        requested_channel=policy_rules.normalize_channel(followup.channel_requested), not_before=followup.not_before_at,
        status=CommunicationJobStatus.PENDING, authorization=authorization,
        idempotency_key=idempotency_key(source_type, followup.id), max_attempts=policy.max_attempts,
        created_at=now, updated_at=now)
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:  # another worker created it first
        existing = job_for_followup(session, source_type, followup_id)
        return IntakeResult(event_id=event.id, result="duplicate", job_id=existing.id if existing else None)
    followup.communication_job_id = job.id
    audit(session, job, "COMMUNICATION_JOB_CREATED", f"Communication job {job.id} created.", now,
          purpose=job.purpose, urgency=job.urgency, authorization=authorization.value, event_id=event.id)
    _start_agent(session, runtime, job, now, organization_id)
    return IntakeResult(event_id=event.id, result="created", job_id=job.id)


def _start_agent(session: Session, runtime: Any, job: CommunicationJob, now: datetime, organization_id: int) -> None:
    """One Communication Agent mission per job, owned by the source Guardian mission's owner (never impersonated:
    an owner without an active membership leaves the job PENDING with OWNER_INACTIVE)."""
    source = session.get(AgentMission, job.source_mission_id) if job.source_mission_id is not None else None
    actor = owner_actor(session, organization_id, source)  # the due-mission worker's identity rebuild if source is not None else None
    if actor is None:
        job.last_error_code = "OWNER_INACTIVE"
        audit(session, job, "COMMUNICATION_JOB_UNASSIGNED", f"Communication job {job.id}: no active owner.", now,
              code="OWNER_INACTIVE")
        return
    mission = runtime.create_mission(session, actor, CreateAgentMission(
        agent_key=COMMUNICATION_AGENT_KEY, max_steps=AGENT_MAX_STEPS, context={"job_id": job.id},
        goal=f"Deliver communication job {job.id} through a permitted channel, following institution policy.",
        success_criteria=["The delivery is verified by its connector's post-condition check.",
                          "Only channels the policy, the student's consent and the providers allow are used."],
        priority="high" if job.urgency == "high" else "normal",
    ), internal=True, commit=False, next_wake_at=now)
    job.agent_mission_id = mission.id


# --- Transitions -------------------------------------------------------------------------------------------------------


def notify(session: Session, job: CommunicationJob, event_type: DomainEventType, now: datetime, **payload: Any) -> None:
    """Report to the job's Communication Agent mission (and wake it) and to the originating Guardian mission (no wake:
    its supervisor drains the event at its next checkpoint). Ids, channel and codes only."""
    body = {"job_id": job.id, "source_type": job.source_type, "followup_id": job.source_followup_id,
            "channel": job.selected_channel, "status": job.status.value, **payload}
    for mission_id, wake in ((job.agent_mission_id, True), (job.source_mission_id, False)):
        if mission_id is None:
            continue
        _events.publish(session, event_type=event_type, actor_account_id=None, subject_type=SUBJECT_MISSION,
                        subject_id=mission_id, now=now, payload=body)
        if wake:
            wake_mission(session, mission_id, now)


def _finish(job: CommunicationJob, status: CommunicationJobStatus, now: datetime, outcome_code: Optional[str]) -> None:
    job.status, job.outcome_code, job.completed_at, job.updated_at = status, outcome_code, now, now
    job.next_attempt_at = None


def _close_followup(session: Session, adapters: Dict[str, SourceAdapter], job: CommunicationJob, status: FollowupStatus,
                    code: Optional[str], now: datetime) -> None:
    try:
        adapter = adapter_for(adapters, job.source_type)
        adapter.close(adapter.load(session, job.source_followup_id), status, job_id=job.id, outcome_code=code, now=now)
    except SourceUnavailable:
        pass  # nothing left to close


def cancel_job(session: Session, adapters: Dict[str, SourceAdapter], job: CommunicationJob, code: str,
               now: datetime) -> None:
    """The source issue was resolved (or is gone) before contact: no delivery; the follow-up is closed CANCELLED."""
    if job.status not in ACTIVE_JOB_STATUSES:
        return
    _finish(job, CommunicationJobStatus.CANCELLED, now, code[:32])
    _close_followup(session, adapters, job, FollowupStatus.CANCELLED, code, now)
    audit(session, job, "COMMUNICATION_JOB_CANCELLED", f"Communication job {job.id} cancelled ({code}).", now, code=code)
    notify(session, job, DomainEventType.COMMUNICATION_CANCELLED, now, code=code)


def fail_job(session: Session, adapters: Dict[str, SourceAdapter], job: CommunicationJob, code: str,
             now: datetime) -> None:
    """No (further) delivery is possible: the follow-up is closed FAILED (never counted as contact)."""
    if job.status not in ACTIVE_JOB_STATUSES:
        return
    _finish(job, CommunicationJobStatus.FAILED, now, code[:32])
    job.last_error_code = code[:64]
    _close_followup(session, adapters, job, FollowupStatus.FAILED, code, now)
    audit(session, job, "COMMUNICATION_JOB_FAILED", f"Communication job {job.id} failed ({code}).", now, code=code)
    notify(session, job, DomainEventType.COMMUNICATION_FAILED, now, code=code)


def mark_delivered(session: Session, adapters: Dict[str, SourceAdapter], job: CommunicationJob, outcome_code: str,
                   now: datetime) -> None:
    """Called only after the connector's post-condition check passed."""
    _finish(job, CommunicationJobStatus.DELIVERED, now, outcome_code[:32])
    job.last_error_code = None
    _close_followup(session, adapters, job, FollowupStatus.DELIVERED, outcome_code, now)
    audit(session, job, "COMMUNICATION_DELIVERED", f"Communication job {job.id} delivered ({job.selected_channel}).",
          now, channel=job.selected_channel, outcome_code=outcome_code, attempt_count=job.attempt_count)
    notify(session, job, DomainEventType.COMMUNICATION_DELIVERED, now, outcome_code=outcome_code)


@dataclass(frozen=True)
class ChannelChoice:
    ok: bool
    code: str  # READY or a refusal code


def channel_options(session: Session, job: CommunicationJob, available: List[str],
                    policy: policy_rules.CommunicationPolicy) -> Dict[str, Optional[str]]:
    """Every channel -> None when the agent may choose it now, else the refusal code (purpose, consent, provider,
    switching after a failure). The student's preferred channel, when permitted, is listed first."""
    consent = consent_for(session, job.recipient_student_id)
    order = policy_rules.permitted_channels(job.source_type, job.purpose, consent, available)
    order += [c for c in policy_rules.CHANNEL_ORDER if c not in order]
    return {channel: policy_rules.check_channel(
        channel, source_type=job.source_type, purpose=job.purpose, consent=consent, available=available,
        previous_channel=job.selected_channel, previous_failed=job.attempt_count > 0, policy=policy) for channel in order}


def select_channel(session: Session, job: CommunicationJob, channel: str, available: List[str],
                   policy: policy_rules.CommunicationPolicy, now: datetime) -> ChannelChoice:
    """The Communication Agent's request: check it in code (purpose, consent, provider, no silent switch after a
    failure) and, when allowed, make the job READY for the delivery worker. Nothing is delivered here."""
    if job.status != CommunicationJobStatus.PENDING:
        return ChannelChoice(False, "JOB_NOT_PENDING")
    if job.authorization in (CommunicationAuthorization.APPROVAL_REQUIRED, CommunicationAuthorization.REJECTED):
        return ChannelChoice(False, "APPROVAL_REQUIRED" if job.authorization == CommunicationAuthorization.APPROVAL_REQUIRED
                             else "APPROVAL_REJECTED")
    refusal = channel_options(session, job, available, policy).get(channel, "UNKNOWN_CHANNEL")
    if refusal is not None:
        audit(session, job, "COMMUNICATION_CHANNEL_REFUSED", f"Communication job {job.id}: {channel} refused ({refusal}).",
              now, channel=channel, code=refusal)
        return ChannelChoice(False, refusal)
    job.selected_channel, job.status, job.updated_at = channel, CommunicationJobStatus.READY, now
    job.next_attempt_at = max(t for t in (now, job.not_before) if t is not None)
    audit(session, job, "COMMUNICATION_CHANNEL_SELECTED", f"Communication job {job.id}: {channel} selected.", now,
          channel=channel)
    return ChannelChoice(True, "READY")
