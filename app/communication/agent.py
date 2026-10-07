"""Communication Agent (AgentOS V2 Phase 5): one autonomous mission per communication job.

Created only by the intake of a COMMUNICATION_REQUESTED event (``user_creatable=False``, ``accepts_delegation=False``),
advanced by the due-mission worker (``autonomous=True``). Its brain may only:

* inspect the job (``get_communication_job``) and the channels it may use (``get_allowed_channels``: the purpose
  allowlist AND the student's consent AND provider availability AND no silent switch after a failure);
* request a controlled delivery on one of them (``request_delivery``, an internal request tool: code re-checks the
  channel and marks the job READY; nothing is sent by the agent -- the delivery worker sends through a connector);
* WAIT for the outcome, or REPLAN.

``CommunicationSupervisor`` (code) owns every terminal outcome: COMPLETE only when the job is DELIVERED and the
delivery is verified again from recorded state (a delivered attempt whose connector post-check passes); FAIL when the
job failed or no channel is permitted; CANCEL when the source issue was resolved first. A brain COMPLETE/FAIL is
never trusted. Tool outputs carry ids, channel names and codes only -- never a name, phone number or e-mail address.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar, Dict, Literal, Optional, Tuple, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agentos.events import SUBJECT_MISSION
from app.agentos.registry import AgentRegistry, AgentSpec, AgentTool, ToolContext, ToolExecutionError, ToolRegistry, ToolResult
from app.agentos.schemas import DecisionKind, DomainEventType
from app.agentos.supervisor import SupervisorVerdict, Trigger
from app.communication import service
from app.communication.base import DeliveryRequest, DeliveryResult
from app.communication.connectors import ConnectorRegistry
from app.communication.sources import SourceAdapter, SourceUnavailable, adapter_for
from app.db.models.agent_kernel import AgentMission, DomainEvent
from app.db.models.communication_delivery import (
    AttemptStatus, CommunicationAttempt, CommunicationAuthorization, CommunicationJob, CommunicationJobStatus,
)
from app.db.models.workflow import OperationAuditEvent
from app.rules import communication_policy as policy_rules
from app.schemas.enums import UserRole

AGENT_KEY = service.COMMUNICATION_AGENT_KEY
AGENT_ROLES = frozenset({UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN})  # the Guardian owners
WAIT_EVENTS = frozenset({DomainEventType.COMMUNICATION_DELIVERED, DomainEventType.COMMUNICATION_FAILED,
                         DomainEventType.COMMUNICATION_CANCELLED, DomainEventType.HUMAN_RESPONDED})
CHOOSE_AGAIN = timedelta(minutes=15)  # a brain that waited without choosing is asked again after this
HEARTBEAT = timedelta(hours=6)  # re-verify a job in flight even if no outcome event arrived


def mission_job(session: Session, mission: Optional[AgentMission]) -> Optional[CommunicationJob]:
    """The job this mission was created for -- and only if the job points back at the mission."""
    if mission is None or mission.agent_key != AGENT_KEY:
        return None
    job_id = ((mission.context or {}).get("inputs") or {}).get("job_id")
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        return None
    job = session.get(CommunicationJob, job_id)
    return job if job is not None and job.agent_mission_id == mission.id else None


def _bound(context: ToolContext) -> CommunicationJob:
    job = mission_job(context.session, context.session.get(AgentMission, context.mission_id))
    if job is None:
        raise ToolExecutionError("JOB_NOT_BOUND")
    return job


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeliveryRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Literal["in_app", "email", "voice", "sms", "whatsapp"]


class _CommunicationTool(AgentTool):
    required_roles = AGENT_ROLES

    def __init__(self, connectors: ConnectorRegistry, policy: policy_rules.CommunicationPolicy) -> None:
        self.connectors, self.policy = connectors, policy


class GetCommunicationJob(_CommunicationTool):
    name: ClassVar[str] = "get_communication_job"
    description: ClassVar[str] = ("This mission's communication job: purpose, urgency, status, requested and selected "
                                  "channel, attempts, authorization and the last error code.")
    input_model = NoInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        job = _bound(context)
        return ToolResult(ok=True, data={
            "job_id": job.id, "source_type": job.source_type, "purpose": job.purpose, "urgency": job.urgency,
            "status": job.status.value, "authorization": job.authorization.value,
            "requested_channel": job.requested_channel, "selected_channel": job.selected_channel,
            "attempt_count": job.attempt_count, "max_attempts": job.max_attempts, "not_before": _iso(job.not_before),
            "next_attempt_at": _iso(job.next_attempt_at), "last_error_code": job.last_error_code})


class GetAllowedChannels(_CommunicationTool):
    name: ClassVar[str] = "get_allowed_channels"
    description: ClassVar[str] = ("The channels this job may use now (policy, the student's consent and provider "
                                  "availability), in preference order, and the refusal code of every other channel.")
    input_model = NoInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        job = _bound(context)
        options = service.channel_options(context.session, job, self.connectors.available_channels(), self.policy)
        return ToolResult(ok=True, data={
            "permitted": [c for c, refusal in options.items() if refusal is None],
            "refused": {c: refusal for c, refusal in options.items() if refusal is not None},
            "requested_channel": job.requested_channel, "fallback_allowed": self.policy.allow_channel_fallback})


class RequestDelivery(_CommunicationTool):
    """Internal request tool: code re-checks the channel and marks the job READY. Nothing is delivered here."""

    name: ClassVar[str] = "request_delivery"
    description: ClassVar[str] = ("Request delivery of this job on one permitted channel. Policy re-checks it; the "
                                  "delivery worker then sends it through the channel's connector.")
    input_model = DeliveryRequestInput
    request_models = frozenset({CommunicationJob, OperationAuditEvent})

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        job = _bound(context)
        channel = cast(DeliveryRequestInput, args).channel
        choice = service.select_channel(context.session, job, channel, self.connectors.available_channels(),
                                        self.policy, context.now)
        if not choice.ok:
            raise ToolExecutionError(choice.code)
        return ToolResult(ok=True, data={"job_id": job.id, "channel": channel, "status": job.status.value,
                                         "next_attempt_at": _iso(job.next_attempt_at)})


TOOL_CLASSES = (GetCommunicationJob, GetAllowedChannels, RequestDelivery)


# --- Deterministic verification ------------------------------------------------------------------------------------------


def verify_delivery(session: Session, job: CommunicationJob, connectors: ConnectorRegistry,
                    adapters: Dict[str, SourceAdapter]) -> bool:
    """Re-verify a DELIVERED job from recorded state: its latest attempt is DELIVERED on the job's channel, and that
    channel's connector post-check confirms the delivery (for in-app: the notification row exists)."""
    attempt = session.execute(select(CommunicationAttempt).where(CommunicationAttempt.job_id == job.id)
                              .order_by(CommunicationAttempt.attempt_number.desc()).limit(1)).scalars().first()
    connector = connectors.get(job.selected_channel)
    if attempt is None or connector is None or attempt.status != AttemptStatus.DELIVERED \
            or attempt.channel != job.selected_channel:
        return False
    try:
        adapter = adapter_for(adapters, job.source_type)
        facts = adapter.facts(session, adapter.load(session, job.source_followup_id))
    except SourceUnavailable:
        return False
    request = DeliveryRequest(organization_id=job.organization_id, job_id=job.id, attempt_id=attempt.id,
                              attempt_number=attempt.attempt_number, student_id=job.recipient_student_id,
                              source_type=job.source_type, purpose=job.purpose, urgency=job.urgency, facts=facts,
                              now=attempt.completed_at or attempt.started_at)
    return connector.verify(session, request, DeliveryResult("delivered", provider_reference=attempt.provider_reference,
                                                             outcome_code=attempt.outcome_code))


class CommunicationSupervisor:
    def __init__(self, connectors: ConnectorRegistry, adapters: Dict[str, SourceAdapter],
                 policy: policy_rules.CommunicationPolicy) -> None:
        self.connectors, self.adapters, self.policy = connectors, adapters, policy

    @staticmethod
    def _drain(session: Session, mission: AgentMission, now: datetime) -> None:
        """Outcome events only signal a wake; the job's state comes from SQL."""
        for event in session.execute(select(DomainEvent).where(
                DomainEvent.consumed_at.is_(None), DomainEvent.subject_type == SUBJECT_MISSION,
                DomainEvent.subject_id == str(mission.id))).scalars():
            event.consumed_at = now

    def review(self, session: Session, mission: AgentMission, now: datetime, trigger: Trigger) -> SupervisorVerdict:
        self._drain(session, mission, now)
        job = mission_job(session, mission)
        if job is None:
            return SupervisorVerdict("fail", code="JOB_NOT_FOUND", outcome={"result": "JOB_NOT_FOUND"})
        base = {"job_id": job.id, "channel": job.selected_channel, "attempt_count": job.attempt_count}
        if job.status in (CommunicationJobStatus.DELIVERED, CommunicationJobStatus.ACKNOWLEDGED):
            if verify_delivery(session, job, self.connectors, self.adapters):
                return SupervisorVerdict("complete", code=job.outcome_code or "DELIVERED",
                                         outcome={"result": job.outcome_code or "DELIVERED", **base})
            return SupervisorVerdict("fail", code="DELIVERY_NOT_VERIFIED", outcome={"result": "DELIVERY_NOT_VERIFIED", **base})
        if job.status == CommunicationJobStatus.FAILED:
            code = job.outcome_code or "DELIVERY_FAILED"
            return SupervisorVerdict("fail", code=code, outcome={"result": code, **base})
        if job.status == CommunicationJobStatus.CANCELLED:
            code = job.outcome_code or "CANCELLED"
            return SupervisorVerdict("cancel", code=code, outcome={"result": code, **base})

        try:  # the source is re-checked on every review: a resolved issue cancels the job before any contact
            adapter = adapter_for(self.adapters, job.source_type)
            closed = adapter.closed_reason(session, adapter.load(session, job.source_followup_id), now)
        except SourceUnavailable as exc:
            closed = exc.code
        if closed is not None:
            service.cancel_job(session, self.adapters, job, closed, now)
            return SupervisorVerdict("cancel", code=closed, outcome={"result": closed, **base})
        if job.authorization == CommunicationAuthorization.REJECTED:
            service.fail_job(session, self.adapters, job, "APPROVAL_REJECTED", now)
            return SupervisorVerdict("fail", code="APPROVAL_REJECTED", outcome={"result": "APPROVAL_REJECTED", **base})
        if job.authorization == CommunicationAuthorization.APPROVAL_REQUIRED:
            return SupervisorVerdict("wait", waiting_for=DomainEventType.HUMAN_RESPONDED, wake_at=None)
        if job.status != CommunicationJobStatus.PENDING:  # READY / IN_PROGRESS / DEFERRED: the delivery worker owns it
            return SupervisorVerdict("wait", waiting_for=DomainEventType.COMMUNICATION_DELIVERED, wake_at=now + HEARTBEAT)

        options = service.channel_options(session, job, self.connectors.available_channels(), self.policy)
        permitted = [c for c, refusal in options.items() if refusal is None]
        if not permitted:
            service.fail_job(session, self.adapters, job, "NO_PERMITTED_CHANNEL", now)
            return SupervisorVerdict("fail", code="NO_PERMITTED_CHANNEL", outcome={"result": "NO_PERMITTED_CHANNEL", **base})
        return SupervisorVerdict("continue", state={
            "job_id": job.id, "purpose": job.purpose, "urgency": job.urgency, "permitted_channels": permitted,
            "requested_channel": job.requested_channel, "last_error_code": job.last_error_code})

    def plan_wait(self, session: Session, mission: AgentMission, requested_wake: Optional[datetime],
                  now: datetime) -> Tuple[DomainEventType, Optional[datetime]]:
        job = mission_job(session, mission)
        if job is not None and job.status == CommunicationJobStatus.PENDING:
            return DomainEventType.COMMUNICATION_DELIVERED, now + CHOOSE_AGAIN  # nothing chosen yet: ask again later
        return DomainEventType.COMMUNICATION_DELIVERED, now + HEARTBEAT


def agent_spec(connectors: ConnectorRegistry, adapters: Dict[str, SourceAdapter],
               policy: policy_rules.CommunicationPolicy) -> AgentSpec:
    return AgentSpec(
        AGENT_KEY, "Delivers one communication job through a permitted channel and verifies the delivery.",
        allowed_tools=frozenset(t.name for t in TOOL_CLASSES), supported_roles=AGENT_ROLES,
        allowed_decisions=frozenset({DecisionKind.TOOL, DecisionKind.WAIT, DecisionKind.REPLAN, DecisionKind.COMPLETE,
                                     DecisionKind.FAIL}),
        wait_events=WAIT_EVENTS, accepts_delegation=False, user_creatable=False, autonomous=True,
        supervisor=CommunicationSupervisor(connectors, adapters, policy),
    )


def register_communication_agent(agents: AgentRegistry, tools: ToolRegistry, connectors: ConnectorRegistry,
                                 adapters: Dict[str, SourceAdapter], policy: policy_rules.CommunicationPolicy) -> None:
    for tool_class in TOOL_CLASSES:
        tools.register(tool_class(connectors, policy))
    agents.register(agent_spec(connectors, adapters, policy))
