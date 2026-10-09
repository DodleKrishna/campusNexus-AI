"""Attendance Guardian (AgentOS V2 Phase 4): one persistent AgentRuntime agent per confirmed unexplained absence.

Created only by deterministic absence detection (``app.services.attendance_monitor``; ``user_creatable=False``),
owned by the class's faculty account and advanced by the due-mission worker (``autonomous=True``). Every tool is
bound to the mission's own intervention (``intervention.mission_id == mission.id``): no input can name another
student or class, and no output carries a name, phone number or e-mail address.

observe -> validate absence -> request communication if policy allows -> WAIT for a relevant event -> re-observe:

* ``AttendanceSupervisor`` (code) resolves the intervention from recorded facts only -- an attending mark
  (ATTENDANCE_CORRECTED / ATTENDANCE_RECORDED), an EXCUSED mark or approved justification (ATTENDANCE_JUSTIFIED),
  approved leave covering the class (LEAVE_APPROVED) -- and ends the mission COMPLETED; it ends it FAILED
  (``ABSENCE_UNRESOLVED``) once the resolution window closes. Events between checkpoints, and any checkpoint at
  which policy would allow no follow-up, need no AI call.
* The AgentBrain is asked only when a follow-up could be allowed. ``request_attendance_followup`` is an internal
  request tool decided by ``evaluate_attendance_followup`` (cooldown, max attempts, quiet hours, active request).
  The brain can never invent attendance or a resolution: only the supervisor ends the mission.
"""
from __future__ import annotations

import os

from datetime import datetime, timedelta
from typing import Any, ClassVar, List, Literal, Optional, Tuple, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agentos import policy_env
from app.agentos.events import SUBJECT_MISSION
from app.agentos.registry import AgentRegistry, AgentSpec, AgentTool, ToolContext, ToolExecutionError, ToolRegistry, ToolResult
from app.agentos.schemas import DecisionKind, DomainEventType
from app.agentos.supervisor import SupervisorVerdict, Trigger
from app.db.models.academic import AttendanceRecord, Enrollment
from app.db.models.agent_kernel import AgentMission, DomainEvent
from app.db.models.attendance_intervention import AttendanceFollowup, AttendanceIntervention, InterventionStatus
from app.db.models.faculty import AttendanceSession, TeachingAssignment
from app.db.models.workflow import OperationAuditEvent
from app.rules import attendance_intervention as rules
from app.rules.followup_policy import FollowupPolicy
from app.schemas.enums import UserRole
from app.services import attendance_monitor as service
from app.services.class_schedule import CAMPUS_TZ

GUARDIAN_AGENT_KEY = service.GUARDIAN_AGENT_KEY
GUARDIAN_ROLES = frozenset({UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN})
MIN_WAKE = timedelta(minutes=15)
CHECKPOINT_KEY = "guardian_checkpoint"
WAIT_EVENTS = frozenset({DomainEventType.ATTENDANCE_CORRECTED, DomainEventType.ATTENDANCE_JUSTIFIED,
                         DomainEventType.LEAVE_APPROVED})

ENV_GRACE = "CAMPUSNEXUS_ATTENDANCE_ABSENCE_GRACE_MINUTES"  # 15
MIN_PRODUCTION_GRACE_MINUTES = 5  # a shorter grace period is a demo-only configuration (CAMPUSNEXUS_DEMO_MODE=1)
ENV_COOLDOWN = "CAMPUSNEXUS_ATTENDANCE_FOLLOWUP_COOLDOWN_MINUTES"  # 360
ENV_MAX_ATTEMPTS = "CAMPUSNEXUS_ATTENDANCE_FOLLOWUP_MAX_ATTEMPTS"  # 3
ENV_WINDOW_HOURS = "CAMPUSNEXUS_ATTENDANCE_RESOLUTION_HOURS"  # 72


def policy_from_env() -> rules.AttendancePolicy:
    quiet_start, quiet_end = policy_env.quiet_hours()
    contact = FollowupPolicy(cooldown=timedelta(minutes=policy_env.number(ENV_COOLDOWN, 360)),
                             max_attempts=int(policy_env.number(ENV_MAX_ATTEMPTS, 3)), quiet_start=quiet_start,
                             quiet_end=quiet_end, timezone=CAMPUS_TZ)
    grace = policy_env.number(ENV_GRACE, 15)
    if grace < MIN_PRODUCTION_GRACE_MINUTES and (os.environ.get("CAMPUSNEXUS_DEMO_MODE") or "").strip() != "1":
        raise ValueError(f"{ENV_GRACE} below {MIN_PRODUCTION_GRACE_MINUTES} needs CAMPUSNEXUS_DEMO_MODE=1")
    return rules.AttendancePolicy(contact=contact, grace=timedelta(minutes=grace),
                                  resolution_window=timedelta(hours=policy_env.number(ENV_WINDOW_HOURS, 72)))


def mission_intervention(session: Session, mission: Optional[AgentMission]) -> Optional[AttendanceIntervention]:
    """The intervention this Guardian mission was created for -- and only if it points back at the mission."""
    if mission is None or mission.agent_key != GUARDIAN_AGENT_KEY:
        return None
    intervention_id = ((mission.context or {}).get("inputs") or {}).get("intervention_id")
    if not isinstance(intervention_id, int) or isinstance(intervention_id, bool):
        return None
    intervention = session.get(AttendanceIntervention, intervention_id)
    return intervention if intervention is not None and intervention.mission_id == mission.id else None


def _bound(context: ToolContext) -> Tuple[AgentMission, AttendanceIntervention]:
    mission = context.session.get(AgentMission, context.mission_id)
    intervention = mission_intervention(context.session, mission)
    if intervention is None:
        raise ToolExecutionError("INTERVENTION_NOT_BOUND")
    return cast(AgentMission, mission), intervention


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _followup_decision(session: Session, intervention: AttendanceIntervention, facts: service.CaseFacts,
                       policy: rules.AttendancePolicy, now: datetime) -> rules.AttendanceFollowupDecision:
    state = service.followup_state(session, intervention.id)
    return rules.evaluate_attendance_followup(
        now=now, intervention_open=intervention.status == InterventionStatus.OPEN, resolved_by=facts.resolved_by,
        deadline=facts.deadline, active_followup=state.active, attempts_so_far=state.attempts,
        last_requested_at=state.last_requested_at, policy=policy)


# --- Tools ---------------------------------------------------------------------------------------------------------


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LimitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, ge=1, le=30)


class AttendanceFollowupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_channel: Optional[Literal["in_app", "sms", "voice_call"]] = None


class _GuardianTool(AgentTool):
    required_roles = GUARDIAN_ROLES

    def __init__(self, policy: rules.AttendancePolicy) -> None:
        self.policy = policy


class GetAttendanceCase(_GuardianTool):
    name: ClassVar[str] = "get_attendance_case"
    description: ClassVar[str] = ("This mission's absence case: intervention status, class times and status, the mark at "
                                  "detection and now, approved leave/justification, resolution (if any), deadline.")
    input_model = NoInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, i = _bound(context)
        row = context.session.get(AttendanceSession, i.attendance_session_id)
        facts = service.case_facts(context.session, i, self.policy)
        return ToolResult(ok=True, data={
            "intervention_id": i.id, "status": i.status.value, "student_id": i.student_id,
            "session_id": i.attendance_session_id, "class_status": facts.session_status,
            "class_starts_at": _iso(row.scheduled_start) if row else None,
            "class_ends_at": _iso(row.scheduled_end) if row else None, "detected_mark": i.detected_mark,
            "current_mark": facts.mark, "leave_approved": facts.leave_approved,
            "justification_approved": i.justification_code is not None, "resolved_by": facts.resolved_by,
            "resolution_deadline": _iso(facts.deadline),
            "next_checkpoint": _iso(_next(rules.checkpoints(i.detected_at, self.policy), context.now))})


class GetStudentAttendanceSummary(_GuardianTool):
    name: ClassVar[str] = "get_student_attendance_summary"
    description: ClassVar[str] = ("The absent student's raw counters for this course (classes attended / conducted) and "
                                  "their attendance interventions in it by status. No percentages are computed here.")
    input_model = NoInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, i = _bound(context)
        session = context.session
        teaching = session.get(TeachingAssignment, i.teaching_assignment_id)
        counters = session.execute(select(AttendanceRecord.classes_attended, AttendanceRecord.classes_conducted)
                                   .join(Enrollment, Enrollment.id == AttendanceRecord.enrollment_id)
                                   .where(Enrollment.student_id == i.student_id, Enrollment.course_id == i.course_id,
                                          Enrollment.academic_year == (teaching.academic_term if teaching else ""))).first()
        statuses = list(session.execute(select(AttendanceIntervention.status).where(
            AttendanceIntervention.student_id == i.student_id, AttendanceIntervention.course_id == i.course_id)).scalars())
        return ToolResult(ok=True, data={
            "student_id": i.student_id, "classes_attended": counters[0] if counters else None,
            "classes_conducted": counters[1] if counters else None,
            "interventions": {s.value: sum(1 for x in statuses if x == s) for s in InterventionStatus}})


class GetRecentAttendanceEvents(_GuardianTool):
    name: ClassVar[str] = "get_recent_attendance_events"
    description: ClassVar[str] = "Recent attendance events for this student in this class session and this case (types, times)."
    input_model = LimitInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        mission, i = _bound(context)
        limit = cast(LimitInput, args).limit
        session_rows = context.session.execute(select(DomainEvent).where(
            DomainEvent.subject_type == service.SUBJECT_SESSION, DomainEvent.subject_id == str(i.attendance_session_id))
            .order_by(DomainEvent.id.desc()).limit(200)).scalars()
        case_events = context.session.execute(select(DomainEvent).where(
            DomainEvent.subject_type == SUBJECT_MISSION, DomainEvent.subject_id == str(mission.id))
            .order_by(DomainEvent.id.desc()).limit(limit)).scalars()
        mine = [e for e in session_rows if (e.payload or {}).get("student_id") in (None, i.student_id)][:limit]
        return ToolResult(ok=True, data={
            "class_events": [{"type": e.event_type, "status": (e.payload or {}).get("status"), "at": _iso(e.created_at)}
                             for e in mine],
            "case_events": [{"type": e.event_type, "at": _iso(e.created_at)} for e in case_events]})


class GetAttendanceFollowupState(_GuardianTool):
    name: ClassVar[str] = "get_attendance_followup_state"
    description: ClassVar[str] = "Follow-up attempts for this case and whether policy would allow one now (reason code)."
    input_model = NoInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, i = _bound(context)
        facts = service.case_facts(context.session, i, self.policy)
        state = service.followup_state(context.session, i.id)
        decision = _followup_decision(context.session, i, facts, self.policy, context.now)
        return ToolResult(ok=True, data={
            "attempts": state.attempts, "max_attempts": self.policy.contact.max_attempts, "active_followup": state.active,
            "last_requested_at": _iso(state.last_requested_at), "followup_allowed_now": decision.allowed,
            "reason": decision.reason})


class RequestAttendanceFollowup(_GuardianTool):
    """Internal request tool: asks for a follow-up with the absent student; code decides. Declared writes only."""

    name: ClassVar[str] = "request_attendance_followup"
    description: ClassVar[str] = ("Request contact with this case's absent student (no message is sent now). Checked by "
                                  "institution policy; the result says requested or suppressed + reason.")
    input_model = AttendanceFollowupInput
    request_models = frozenset({AttendanceFollowup, DomainEvent, OperationAuditEvent})

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        mission, i = _bound(context)
        result = service.request_followup(
            context.session, mission=mission, intervention=i,
            preferred_channel=cast(AttendanceFollowupInput, args).preferred_channel, policy=self.policy, now=context.now,
            actor_account_id=context.account_id)
        return ToolResult(ok=True, data=result)


TOOL_CLASSES = (GetAttendanceCase, GetStudentAttendanceSummary, GetRecentAttendanceEvents, GetAttendanceFollowupState,
                RequestAttendanceFollowup)


def _next(points: List[datetime], now: datetime) -> Optional[datetime]:
    return next((p for p in sorted(points) if p > now), None)


# --- Supervisor ----------------------------------------------------------------------------------------------------


class AttendanceSupervisor:
    def __init__(self, policy: rules.AttendancePolicy) -> None:
        self.policy = policy

    @staticmethod
    def _drain(session: Session, mission: AgentMission, now: datetime) -> None:
        for event in session.execute(select(DomainEvent).where(
                DomainEvent.consumed_at.is_(None), DomainEvent.subject_type == SUBJECT_MISSION,
                DomainEvent.subject_id == str(mission.id))).scalars():
            event.consumed_at = now

    @staticmethod
    def _audit(session: Session, mission: AgentMission, intervention: AttendanceIntervention, event_type: str,
               message: str, now: datetime, **metadata: Any) -> None:
        service._audit(session, event_type=event_type, actor_account_id=mission.owner_account_id,
                       actor_role=service.AGENT_ROLE, intervention_id=intervention.id, message=message, now=now,
                       mission_id=mission.id, **metadata)

    def _close(self, session: Session, intervention: AttendanceIntervention, status: InterventionStatus,
               code: str, now: datetime) -> int:
        intervention.status, intervention.resolution_code = status, code
        intervention.resolved_at, intervention.updated_at = now, now
        return service.close_active_followups(session, intervention.id, now)

    def review(self, session: Session, mission: AgentMission, now: datetime, trigger: Trigger) -> SupervisorVerdict:
        self._drain(session, mission, now)
        intervention = mission_intervention(session, mission)
        if intervention is None:
            return SupervisorVerdict("fail", code="INTERVENTION_NOT_FOUND", outcome={"result": "INTERVENTION_NOT_FOUND"})
        outcome = {"intervention_id": intervention.id, "student_id": intervention.student_id}
        facts = service.case_facts(session, intervention, self.policy)
        if intervention.status != InterventionStatus.OPEN or facts.session_status in ("cancelled", "missing"):
            if intervention.status == InterventionStatus.OPEN:
                self._close(session, intervention, InterventionStatus.CANCELLED, "CLASS_CANCELLED", now)
            return SupervisorVerdict("cancel", code="INTERVENTION_CLOSED",
                                     outcome={"result": "INTERVENTION_CLOSED", "status": intervention.status.value, **outcome})
        if facts.resolved_by is not None:
            closed = self._close(session, intervention, InterventionStatus.RESOLVED, facts.resolved_by, now)
            self._audit(session, mission, intervention, "ATTENDANCE_RESOLVED",
                        f"Intervention {intervention.id} resolved ({facts.resolved_by}, verified).", now,
                        resolution_code=facts.resolved_by, followups_closed=closed)
            return SupervisorVerdict("complete", code=facts.resolved_by,
                                     outcome={"result": "RESOLVED", "resolution_code": facts.resolved_by, **outcome})
        if now >= facts.deadline + rules.SETTLE:
            closed = self._close(session, intervention, InterventionStatus.UNRESOLVED, "RESOLUTION_WINDOW_CLOSED", now)
            attempts = service.followup_state(session, intervention.id).attempts
            self._audit(session, mission, intervention, "ATTENDANCE_UNRESOLVED",
                        f"Intervention {intervention.id}: resolution window closed unresolved.", now,
                        followups_closed=closed, followup_attempts=attempts)
            return SupervisorVerdict("fail", code="ABSENCE_UNRESOLVED", outcome={
                "result": "ABSENCE_UNRESOLVED", "followup_attempts": attempts, **outcome})

        nxt = self._next_wake(session, intervention, facts, now)
        decision = _followup_decision(session, intervention, facts, self.policy, now)
        if trigger != "running" and not decision.allowed:
            # Nothing policy would allow: no AI call. (An allowed follow-up may be decided on any wake: the policy
            # already enforces cooldown and the attempt limit, so this cannot loop.)
            mission.context = {**(mission.context or {}), CHECKPOINT_KEY: _iso(nxt)}
            return SupervisorVerdict("wait", waiting_for=DomainEventType.ATTENDANCE_CORRECTED, wake_at=nxt)
        return SupervisorVerdict("continue", state={
            "intervention_id": intervention.id, "now": _iso(now), "current_mark": facts.mark,
            "detected_mark": intervention.detected_mark, "leave_approved": facts.leave_approved,
            "followup_allowed_now": decision.allowed, "followup_reason": decision.reason,
            "resolution_deadline": _iso(facts.deadline), "next_checkpoint": _iso(nxt)})

    def _next_wake(self, session: Session, intervention: AttendanceIntervention, facts: service.CaseFacts,
                   now: datetime) -> Optional[datetime]:
        """The next static checkpoint -- or sooner, one cooldown from now, while an undelivered request blocks a new
        attempt that the limits would otherwise still allow (bounded by the resolution deadline; no AI call)."""
        wake = _next(rules.checkpoints(intervention.detected_at, self.policy), now)
        state = service.followup_state(session, intervention.id)
        if state.active and state.attempts < self.policy.contact.max_attempts:
            retry = now + self.policy.contact.cooldown
            if retry < facts.deadline and (wake is None or retry < wake):
                wake = retry
        return wake

    def plan_wait(self, session: Session, mission: AgentMission, requested_wake: Optional[datetime],
                  now: datetime) -> Tuple[DomainEventType, Optional[datetime]]:
        intervention = mission_intervention(session, mission)
        wake = (self._next_wake(session, intervention, service.case_facts(session, intervention, self.policy), now)
                if intervention is not None else None)
        if requested_wake is not None and wake is not None and now + MIN_WAKE <= requested_wake < wake:
            wake = requested_wake
        pending = session.execute(select(DomainEvent.id).where(
            DomainEvent.consumed_at.is_(None), DomainEvent.subject_type == SUBJECT_MISSION,
            DomainEvent.subject_id == str(mission.id))).first()
        mission.context = {**(mission.context or {}), CHECKPOINT_KEY: _iso(wake)}
        return DomainEventType.ATTENDANCE_CORRECTED, (now if pending is not None else wake)


# --- Registration --------------------------------------------------------------------------------------------------


def guardian_spec(policy: rules.AttendancePolicy) -> AgentSpec:
    return AgentSpec(
        GUARDIAN_AGENT_KEY, "Follows up one confirmed unexplained class absence until it is resolved or the window closes.",
        allowed_tools=frozenset(t.name for t in TOOL_CLASSES), supported_roles=GUARDIAN_ROLES,
        allowed_decisions=frozenset({DecisionKind.TOOL, DecisionKind.WAIT, DecisionKind.COMPLETE, DecisionKind.FAIL,
                                     DecisionKind.REPLAN}),
        wait_events=WAIT_EVENTS, accepts_delegation=False, user_creatable=False, autonomous=True,
        supervisor=AttendanceSupervisor(policy),
    )


def register_attendance_guardian(agents: AgentRegistry, tools: ToolRegistry,
                                 policy: Optional[rules.AttendancePolicy] = None) -> None:
    policy = policy or policy_from_env()
    for tool_class in TOOL_CLASSES:
        tools.register(tool_class(policy))
    agents.register(guardian_spec(policy))


def scan_attendance(factory: Any, runtime: Any, *, now: Optional[datetime] = None,
                    policy: Optional[rules.AttendancePolicy] = None, limit: int = service.MAX_SCAN_EVENTS) -> Any:
    """The deterministic absence-detection pass (run beside ``process_due_missions``). Uses the runtime's registered
    attendance policy so detection and the Guardian agree on the grace period."""
    spec = runtime.agents.get(GUARDIAN_AGENT_KEY)
    policy = policy or (spec.supervisor.policy if spec is not None else policy_from_env())
    return service.process_attendance_events(factory, runtime, policy, now=now, limit=limit)
