"""Communication worker (AgentOS V2 Phase 5): intake, then delivery of due jobs. No AI call anywhere here.

``process_communication_jobs`` runs, per active organization in its *tenant* session:

1. intake -- ``service.consume_requests``: pending COMMUNICATION_REQUESTED events -> one job (+ one Communication
   Agent mission) per follow-up, idempotently;
2. delivery -- READY / DEFERRED jobs whose ``next_attempt_at`` has come, each claimed by one conditional UPDATE
   (lease owner + expiry; portable, no ``FOR UPDATE``) so two workers never deliver the same job. For each:
   reload the source (resolved -> CANCELLED, follow-up closed, no contact), re-check the channel (consent may have
   changed -> back to PENDING for the agent), apply ``check_delivery`` (approval, attempts, not-before/backoff, quiet
   hours -> DEFERRED), then exactly one attempt through the channel's connector inside a savepoint. A delivery counts
   only when the connector's post-condition check confirms it; a failure is retried with backoff or fails the job
   (``after_failed_attempt``). The outcome is reported to the Communication Agent mission (which is woken) and to the
   Guardian mission.

Bounded: at most ``limit`` intake events and ``limit`` deliveries per call; each job is attempted at most once per
call. Nothing loops until a condition holds.

Phase 6 (offline edge): with a ``ConnectivityService`` (``runtime.connectivity``) each organization first records a
connectivity transition (NETWORK_LOST / NETWORK_RESTORED, only on change). While the deployment is ``local_only``, a
due job whose connector ``requires_internet`` is not attempted: it becomes DEFERRED with ``last_error_code =
WAITING_CONNECTIVITY`` and no wake time, is excluded from every due query (no retry loop, no attempt counted, never
marked delivered) and its source mission keeps running. In-app delivery is unaffected. A NETWORK_RESTORED event
releases the waiting jobs (bounded batch, staggered ``limit`` per ``RELEASE_SPACING``) so a reconnect never causes a
delivery storm; the event is consumed once every waiting job has been released.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel
from sqlalchemy import or_, select, update

from app.agentos.connectivity import SUBJECT_CONNECTIVITY, ConnectivityState, publish_transition
from app.agentos.events import EventService
from app.agentos.schemas import DomainEventType
from app.communication import service
from app.communication.base import DeliveryRequest, DeliveryResult
from app.communication.connectors import ConnectorRegistry
from app.communication.sources import SourceAdapter, SourceUnavailable, adapter_for, build_adapters
from app.db.base import utc_now
from app.db.models.communication_delivery import (
    AttemptStatus, CommunicationAttempt, CommunicationJob, CommunicationJobStatus, VoiceSession, VoiceSessionStatus,
)
from app.db.models.organization import Organization
from app.db.tenant_session import TenantSessionFactory
from app.rules import communication_policy as policy_rules
from app.schemas.enums import OrganizationStatus

MAX_BATCH = 100
DEFAULT_LIMIT = 20
LEASE = timedelta(minutes=5)
DUE_STATUSES = [CommunicationJobStatus.READY, CommunicationJobStatus.DEFERRED]
WAITING_CONNECTIVITY = "WAITING_CONNECTIVITY"
RELEASE_BATCH = 200  # waiting jobs released per organization per run
RELEASE_SPACING = timedelta(minutes=1)  # released jobs become due ``limit`` at a time, this far apart
_events = EventService()


def _due(now: datetime) -> list:
    """Conditions of a job the delivery worker may take now. A job waiting for connectivity is never due: only a
    NETWORK_RESTORED release makes it eligible again."""
    return [
        CommunicationJob.status.in_(DUE_STATUSES),
        or_(CommunicationJob.next_attempt_at.is_(None), CommunicationJob.next_attempt_at <= now),
        or_(CommunicationJob.lease_expires_at.is_(None), CommunicationJob.lease_expires_at <= now),
        or_(CommunicationJob.last_error_code.is_(None), CommunicationJob.last_error_code != WAITING_CONNECTIVITY),
    ]


class JobRun(BaseModel):
    organization_id: int
    job_id: int
    outcome: Literal["delivered", "in_progress", "deferred", "retry_scheduled", "failed", "cancelled",
                     "returned_to_agent", "waiting_approval", "call_timed_out", "not_claimed", "error",
                     "waiting_connectivity"]
    code: Optional[str] = None


class CommunicationReport(BaseModel):
    worker_id: str
    connectivity: Optional[str] = None  # cloud_reachable | local_only | unknown (None: not checked)
    connectivity_events: List[str] = []  # NETWORK_LOST / NETWORK_RESTORED published this run
    released: int = 0  # jobs released from WAITING_CONNECTIVITY this run
    intake: List[service.IntakeResult] = []
    runs: List[JobRun] = []


def claim_job(session: Any, job_id: int, worker_id: str, now: datetime, lease: timedelta = LEASE) -> bool:
    result = session.execute(update(CommunicationJob).where(CommunicationJob.id == job_id, *_due(now)).values(lease_owner=worker_id, lease_expires_at=now + lease).execution_options(synchronize_session=False))
    session.commit()
    return result.rowcount == 1


def process_communication_jobs(
    factory: Any, runtime: Any, *, connectors: Optional[ConnectorRegistry] = None,
    adapters: Optional[Dict[str, SourceAdapter]] = None, policy: Optional[policy_rules.CommunicationPolicy] = None,
    limit: int = DEFAULT_LIMIT, now: Optional[datetime] = None, worker_id: Optional[str] = None,
    connectivity: Any = None,
) -> CommunicationReport:
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f"limit must be 1-{MAX_BATCH}")
    factory = TenantSessionFactory.from_sessionmaker(factory)
    setup = getattr(runtime, "communication", None)  # the configuration the Communication Agent was registered with
    connectors = connectors or (setup.connectors if setup else ConnectorRegistry())
    adapters = adapters or (setup.adapters if setup else build_adapters())
    policy = policy or (setup.policy if setup else service.policy_from_env())
    now = now or getattr(runtime, "clock", utc_now)()
    report = CommunicationReport(worker_id=(worker_id or f"comm-{uuid.uuid4().hex[:12]}")[:64])
    connectivity = connectivity if connectivity is not None else getattr(runtime, "connectivity", None)
    state = connectivity.state() if connectivity is not None else None  # one (cached, bounded) check per run
    report.connectivity = state.value if state is not None else None
    offline = state == ConnectivityState.LOCAL_ONLY
    with factory() as unbound:  # organizations are global; no tenant data is read here
        organization_ids = list(unbound.execute(select(Organization.id).where(
            Organization.status == OrganizationStatus.ACTIVE).order_by(Organization.id)).scalars())
    for organization_id in organization_ids:
        if state is not None:
            with factory.open_tenant_session(organization_id) as session:
                published = publish_transition(session, state, now, _events)
                if published is not None:
                    report.connectivity_events.append(published.value)
                if state == ConnectivityState.CLOUD_REACHABLE:
                    report.released += release_waiting_jobs(session, now, per_slot=limit)
                session.commit()
        intake_left = limit - len(report.intake)
        if intake_left > 0:
            with factory.open_tenant_session(organization_id) as session:
                report.intake += service.consume_requests(session, runtime, adapters, policy, now,
                                                          organization_id=organization_id, limit=intake_left)
                session.commit()
        remaining = limit - len(report.runs)
        if remaining <= 0:
            continue
        with factory.open_tenant_session(organization_id) as session:
            report.runs += expire_stale_calls(session, organization_id, now, adapters, policy, limit=remaining)
            session.commit()
        remaining = limit - len(report.runs)
        if remaining <= 0:
            continue
        with factory.open_tenant_session(organization_id) as session:
            due = list(session.execute(select(CommunicationJob.id).where(*_due(now)).order_by(CommunicationJob.next_attempt_at, CommunicationJob.id).limit(remaining)).scalars())
        for job_id in due:
            report.runs.append(_deliver_one(factory, organization_id, job_id, report.worker_id, now, connectors,
                                            adapters, policy, offline))
    return report


def release_waiting_jobs(session: Any, now: datetime, *, per_slot: int, spacing: timedelta = RELEASE_SPACING,
                         batch: int = RELEASE_BATCH) -> int:
    """On an unconsumed NETWORK_RESTORED (subject ``connectivity``), make up to ``batch`` jobs that wait for
    connectivity due again, ``per_slot`` at a time ``spacing`` apart. Consumes the event(s) once none are left."""
    restored = _events.pending(session, event_type=DomainEventType.NETWORK_RESTORED.value,
                               subject_type=SUBJECT_CONNECTIVITY)
    if not restored:
        return 0
    waiting = list(session.execute(select(CommunicationJob).where(
        CommunicationJob.status == CommunicationJobStatus.DEFERRED,
        CommunicationJob.last_error_code == WAITING_CONNECTIVITY,
    ).order_by(CommunicationJob.id).limit(batch + 1)).scalars())
    released = waiting[:batch]
    for index, job in enumerate(released):
        job.last_error_code, job.updated_at = None, now
        job.next_attempt_at = now + spacing * (index // max(1, per_slot))
        service.audit(session, job, "COMMUNICATION_CONNECTIVITY_RESTORED",
                      f"Communication job {job.id} released: connectivity restored.", now,
                      next_attempt_at=job.next_attempt_at.isoformat())
    if len(waiting) <= batch:
        for event in restored:
            _events.consume(event, now)
    return len(released)


def _deliver_one(factory: TenantSessionFactory, organization_id: int, job_id: int, worker_id: str, now: datetime,
                 connectors: ConnectorRegistry, adapters: Dict[str, SourceAdapter],
                 policy: policy_rules.CommunicationPolicy, offline: bool = False) -> JobRun:
    run = JobRun(organization_id=organization_id, job_id=job_id, outcome="error")
    with factory.open_tenant_session(organization_id) as session:
        if not claim_job(session, job_id, worker_id, now):
            run.outcome = "not_claimed"
            return run
        job = session.get(CommunicationJob, job_id)
        try:
            run.outcome, run.code = _attempt(session, job, now, connectors, adapters, policy, offline)
        except Exception as exc:  # noqa: BLE001 -- one broken job never stops the batch
            session.rollback()
            job = session.get(CommunicationJob, job_id)
            run.outcome, run.code = "error", f"WORKER_ERROR:{type(exc).__name__}"[:64]
        if job.lease_owner == worker_id:
            job.lease_owner, job.lease_expires_at = None, None
        session.commit()
    return run


def _attempt(session: Any, job: CommunicationJob, now: datetime, connectors: ConnectorRegistry,
             adapters: Dict[str, SourceAdapter], policy: policy_rules.CommunicationPolicy, offline: bool = False) -> tuple:
    try:
        adapter = adapter_for(adapters, job.source_type)
        followup = adapter.load(session, job.source_followup_id)
        closed = adapter.closed_reason(session, followup, now)
        facts = adapter.facts(session, followup)
    except SourceUnavailable as exc:
        closed, facts = exc.code, None
    if closed is not None:
        service.cancel_job(session, adapters, job, closed, now)
        return "cancelled", closed

    available = connectors.available_channels()
    channel = job.selected_channel
    refusal = policy_rules.check_channel(
        channel or "", source_type=job.source_type, purpose=job.purpose,
        consent=service.consent_for(session, job.recipient_student_id), available=available,
        previous_channel=None, previous_failed=False, policy=policy)
    if refusal is not None:  # consent or availability changed since the agent chose: the agent must choose again
        job.status, job.last_error_code, job.next_attempt_at, job.updated_at = (
            CommunicationJobStatus.PENDING, refusal, None, now)
        service.audit(session, job, "COMMUNICATION_CHANNEL_REFUSED",
                      f"Communication job {job.id}: {channel} no longer permitted ({refusal}).", now, channel=channel,
                      code=refusal)
        service.notify(session, job, DomainEventType.COMMUNICATION_FAILED, now, code=refusal)
        return "returned_to_agent", refusal

    not_before = max((t for t in (job.not_before, job.next_attempt_at) if t is not None), default=None)
    check = policy_rules.check_delivery(
        now=now, source_open=True, authorization=job.authorization.value, channel=channel,
        attempts_so_far=job.attempt_count, not_before=not_before,
        consent=service.consent_for(session, job.recipient_student_id), policy=policy)
    if not check.allowed:
        if check.reason in (policy_rules.DeliveryRefusal.NOT_YET.value, policy_rules.DeliveryRefusal.QUIET_HOURS.value):
            job.status, job.next_attempt_at, job.updated_at = CommunicationJobStatus.DEFERRED, check.defer_until, now
            service.audit(session, job, "COMMUNICATION_DEFERRED", f"Communication job {job.id} deferred ({check.reason}).",
                          now, code=check.reason, until=check.defer_until.isoformat() if check.defer_until else None)
            return "deferred", check.reason
        if check.reason == policy_rules.DeliveryRefusal.APPROVAL_PENDING.value:
            job.status, job.next_attempt_at = CommunicationJobStatus.PENDING, None
            return "waiting_approval", check.reason
        service.fail_job(session, adapters, job, check.reason, now)
        return "failed", check.reason

    connector = connectors.get(channel)
    if offline and connector.requires_internet:  # never attempted offline; the source mission is not failed
        job.status, job.last_error_code, job.next_attempt_at, job.updated_at = (
            CommunicationJobStatus.DEFERRED, WAITING_CONNECTIVITY, None, now)
        service.audit(session, job, "COMMUNICATION_WAITING_CONNECTIVITY",
                      f"Communication job {job.id}: {channel} needs connectivity; deferred.", now, channel=channel,
                      code=WAITING_CONNECTIVITY)
        return "waiting_connectivity", WAITING_CONNECTIVITY
    job.attempt_count += 1
    job.status, job.updated_at = CommunicationJobStatus.IN_PROGRESS, now
    attempt = CommunicationAttempt(job_id=job.id, attempt_number=job.attempt_count, channel=channel,
                                   provider=connector.provider, status=AttemptStatus.IN_PROGRESS, started_at=now,
                                   created_at=now, updated_at=now)
    session.add(attempt)
    session.flush()
    request = DeliveryRequest(organization_id=job.organization_id, job_id=job.id, attempt_id=attempt.id,
                              attempt_number=attempt.attempt_number, student_id=job.recipient_student_id,
                              source_type=job.source_type, purpose=job.purpose, urgency=job.urgency, facts=facts, now=now)
    try:
        with session.begin_nested():  # a failing connector leaves none of its rows behind
            result = connector.deliver(session, request)
            if result.status == "delivered" and not connector.verify(session, request, result):
                result = DeliveryResult("failed", provider_reference=result.provider_reference,
                                        error_code="POSTCONDITION_FAILED")
            if result.status == "failed":
                raise _Failed(result)
    except _Failed as failed:
        result = failed.result
    except Exception:  # noqa: BLE001 -- a connector crash is a provider failure, retried by policy
        result = DeliveryResult("failed", error_code="PROVIDER_UNAVAILABLE")
    attempt.provider_reference, attempt.updated_at = result.provider_reference, now
    service.audit(session, job, "COMMUNICATION_ATTEMPTED", f"Communication job {job.id}: attempt {attempt.attempt_number} "
                  f"on {channel} ({result.status}).", now, channel=channel, attempt_number=attempt.attempt_number,
                  result=result.status, error_code=result.error_code)

    if result.status == "delivered":
        attempt.status, attempt.completed_at, attempt.outcome_code = AttemptStatus.DELIVERED, now, result.outcome_code
        service.mark_delivered(session, adapters, job, result.outcome_code or "DELIVERED", now)
        return "delivered", result.outcome_code
    if result.status == "in_progress":  # an asynchronous provider reports the outcome by callback
        job.next_attempt_at = None
        return "in_progress", None
    attempt.status, attempt.completed_at, attempt.error_code = AttemptStatus.FAILED, now, result.error_code
    return record_failed_attempt(session, job, result.error_code, now, adapters, policy)


def expire_stale_calls(session: Any, organization_id: int, now: datetime, adapters: Dict[str, SourceAdapter],
                       policy: policy_rules.CommunicationPolicy, *, limit: int) -> List[JobRun]:
    """An asynchronous attempt (a voice call) whose final provider callback never arrived within the call time limit
    plus ``callback_grace`` fails as CALLBACK_TIMEOUT (retryable). Each attempt is taken by one conditional UPDATE,
    so a late callback (which ignores final attempts) and this sweep never both settle it."""
    cutoff = now - timedelta(seconds=policy.voice_time_limit_seconds) - policy.callback_grace
    stale = list(session.execute(select(CommunicationAttempt.id, CommunicationAttempt.job_id).join(
        CommunicationJob, CommunicationJob.id == CommunicationAttempt.job_id).where(
        CommunicationJob.status == CommunicationJobStatus.IN_PROGRESS,
        CommunicationAttempt.status.in_([AttemptStatus.IN_PROGRESS, AttemptStatus.ANSWERED]),
        CommunicationAttempt.started_at <= cutoff).order_by(CommunicationAttempt.id).limit(limit)).all())
    runs: List[JobRun] = []
    for attempt_id, job_id in stale:
        taken = session.execute(update(CommunicationAttempt).where(
            CommunicationAttempt.id == attempt_id,
            CommunicationAttempt.status.in_([AttemptStatus.IN_PROGRESS, AttemptStatus.ANSWERED]),
        ).values(status=AttemptStatus.FAILED, error_code="CALLBACK_TIMEOUT", completed_at=now, updated_at=now)
            .execution_options(synchronize_session=False))
        if taken.rowcount != 1:
            continue
        for voice in session.execute(select(VoiceSession).where(
                VoiceSession.communication_attempt_id == attempt_id)).scalars():
            voice.status, voice.ended_at = VoiceSessionStatus.FAILED, voice.ended_at or now
        job = session.get(CommunicationJob, job_id)
        outcome, code = record_failed_attempt(session, job, "CALLBACK_TIMEOUT", now, adapters, policy)
        runs.append(JobRun(organization_id=organization_id, job_id=job_id, outcome="call_timed_out",
                           code=code if outcome == "failed" else "CALLBACK_TIMEOUT"))
    return runs


def record_failed_attempt(session: Any, job: CommunicationJob, error_code: Optional[str], now: datetime,
                          adapters: Dict[str, SourceAdapter], policy: policy_rules.CommunicationPolicy) -> tuple:
    """Retry with backoff on the same channel, or fail the job (non-retryable error / attempts exhausted)."""
    decision = policy_rules.after_failed_attempt(now=now, attempt_number=job.attempt_count, error_code=error_code,
                                                 policy=policy)
    job.last_error_code = (error_code or "UNKNOWN")[:64]
    if decision.retry:
        job.status, job.next_attempt_at, job.updated_at = CommunicationJobStatus.DEFERRED, decision.next_attempt_at, now
        service.audit(session, job, "COMMUNICATION_RETRY_SCHEDULED", f"Communication job {job.id}: retry scheduled.", now,
                      code=error_code, next_attempt_at=decision.next_attempt_at.isoformat())
        return "retry_scheduled", error_code
    service.fail_job(session, adapters, job, error_code if decision.reason == "NON_RETRYABLE_ERROR" else decision.reason,
                     now)
    return "failed", job.outcome_code


class _Failed(Exception):
    def __init__(self, result: DeliveryResult) -> None:
        super().__init__(result.error_code)
        self.result = result
