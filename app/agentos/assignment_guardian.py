"""Assignment Guardian (AgentOS V2 Phase 3): a persistent AgentRuntime agent per published assignment.

Created only by publishing (``app.services.assignments.publish``; ``user_creatable=False``)
and advanced by the due-mission worker (``autonomous=True``). Every tool is bound to the
mission's own assignment (``assignment.guardian_mission_id == mission.id``): no input can
name another assignment, and no output carries a name, phone number or e-mail address.

Division of labour (cost control):

* ``GuardianSupervisor`` (code) decides everything SQL can: success (every target submitted),
  cancellation, the deadline, and when to wake (publication, ``checkpoint_hours`` before the
  deadline, the deadline settle point; collapsed for short assignments). A submission event
  between checkpoints is handled without any AI call, and so is a checkpoint at which no
  follow-up could be allowed.
* The AgentBrain is asked only at a checkpoint with something to decide: what to inspect,
  whether to request follow-ups, whether to replan. ``request_student_followup`` is an internal
  request tool: each requested student is decided by ``evaluate_followup`` (cooldown, max
  attempts, submitted, cancelled, deadline, quiet hours) and only an allowed one becomes an
  ``AssignmentFollowup`` + COMMUNICATION_REQUESTED event. Nothing is sent (Phase 5).
* COMPLETE/FAIL are decided by the supervisor only (a brain COMPLETE/FAIL is re-checked, never trusted). A deadline with pending students
  ends the mission FAILED with outcome DEADLINE_REACHED_WITH_PENDING -- never as a success.

Delegation is allowed to ``communication_agent`` only, which is not registered yet: the
brain is not offered it, and the follow-up requests stay persisted for Phase 5.
"""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agentos.events import SUBJECT_MISSION
from app.agentos.registry import AgentRegistry, AgentSpec, AgentTool, ToolContext, ToolExecutionError, ToolRegistry, ToolResult
from app.agentos.schemas import DecisionKind, DomainEventType
from app.agentos.supervisor import SupervisorVerdict, Trigger
from app.db.models.agent_kernel import AgentMission, DomainEvent
from app.db.models.assignment import Assignment, AssignmentFollowup, AssignmentSubmission, AssignmentTarget
from app.db.models.workflow import OperationAuditEvent
from app.rules import assignment_rules as rules
from app.schemas.enums import UserRole
from app.services import assignments as service
from app.services.class_schedule import CAMPUS_TZ

GUARDIAN_AGENT_KEY = service.GUARDIAN_AGENT_KEY
GUARDIAN_ROLES = frozenset({UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN})
COMMUNICATION_AGENT_KEY = "communication_agent"  # Phase 5; deliberately not registered here
MIN_WAKE = timedelta(minutes=15)  # the earliest re-check a brain may ask for
CHECKPOINT_KEY = "guardian_checkpoint"

ENV_COOLDOWN = "CAMPUSNEXUS_ASSIGNMENT_FOLLOWUP_COOLDOWN_MINUTES"
ENV_MAX_ATTEMPTS = "CAMPUSNEXUS_ASSIGNMENT_FOLLOWUP_MAX_ATTEMPTS"
ENV_QUIET_HOURS = "CAMPUSNEXUS_ASSIGNMENT_QUIET_HOURS"  # "21:00-07:00" campus time; "off" disables
ENV_CHECKPOINTS = "CAMPUSNEXUS_ASSIGNMENT_CHECKPOINT_HOURS"  # "24,6,1"


def policy_from_env() -> rules.FollowupPolicy:
    """The follow-up policy from the environment (defaults: 6 h cooldown, 3 attempts, quiet 21:00-07:00 IST,
    checkpoints 24/6/1 h before the deadline). An invalid value raises ``ValueError`` at startup."""
    cooldown = float(os.environ.get(ENV_COOLDOWN) or 360)
    attempts = int(os.environ.get(ENV_MAX_ATTEMPTS) or 3)
    quiet = (os.environ.get(ENV_QUIET_HOURS) or "21:00-07:00").strip().lower()
    if quiet == "off":
        start = end = time(0, 0)
    else:
        start_text, end_text = quiet.split("-")
        start, end = time.fromisoformat(start_text.strip()), time.fromisoformat(end_text.strip())
    hours = tuple(float(h) for h in (os.environ.get(ENV_CHECKPOINTS) or "24,6,1").split(",") if h.strip())
    return rules.FollowupPolicy(cooldown=timedelta(minutes=cooldown), max_attempts=attempts, quiet_start=start,
                                quiet_end=end, timezone=CAMPUS_TZ, checkpoint_hours=hours)


def mission_assignment(session: Session, mission: Optional[AgentMission]) -> Optional[Assignment]:
    """The assignment this Guardian mission was created for -- and only if it points back at the mission."""
    if mission is None or mission.agent_key != GUARDIAN_AGENT_KEY:
        return None
    assignment_id = ((mission.context or {}).get("inputs") or {}).get("assignment_id")
    if not isinstance(assignment_id, int) or isinstance(assignment_id, bool):
        return None
    assignment = session.get(Assignment, assignment_id)
    return assignment if assignment is not None and assignment.guardian_mission_id == mission.id else None


def _bound(context: ToolContext) -> Tuple[AgentMission, Assignment]:
    mission = context.session.get(AgentMission, context.mission_id)
    assignment = mission_assignment(context.session, mission)
    if assignment is None:
        raise ToolExecutionError("ASSIGNMENT_NOT_BOUND")
    return cast(AgentMission, mission), assignment


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


# --- Tools (compact outputs: ids, counts, statuses; never names or contact details) -------------------------------


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LimitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=50)


class StudentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_id: int = Field(ge=1)


class FollowupRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_ids: List[int] = Field(min_length=1, max_length=service.MAX_FOLLOWUPS_PER_REQUEST)
    purpose: Literal["submission_reminder", "deadline_warning"] = "submission_reminder"
    preferred_channel: Optional[Literal["in_app", "sms", "voice_call"]] = None


class _GuardianTool(AgentTool):
    required_roles = GUARDIAN_ROLES

    def __init__(self, policy: rules.FollowupPolicy) -> None:
        self.policy = policy


class GetAssignmentState(_GuardianTool):
    name: ClassVar[str] = "get_assignment_state"
    description: ClassVar[str] = "This mission's assignment: status, deadline, whether it passed, next checkpoint."
    input_model = NoInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, a = _bound(context)
        points = rules.checkpoints(a.published_at or context.now, a.deadline_at, self.policy.checkpoint_hours)
        return ToolResult(ok=True, data={
            "assignment_id": a.id, "status": a.status.value, "published_at": _iso(a.published_at),
            "deadline_at": _iso(a.deadline_at), "deadline_passed": rules.deadline_passed(a.deadline_at, context.now),
            "followup_window_opens": _iso(rules.reminder_window_opens(a.published_at or context.now, a.deadline_at,
                                                                      self.policy.checkpoint_hours)),
            "next_checkpoint": _iso(rules.next_checkpoint(points, context.now))})


class GetAssignmentProgress(_GuardianTool):
    name: ClassVar[str] = "get_assignment_progress"
    description: ClassVar[str] = "Counts: targets, submitted, late, pending; whether all targets submitted."
    input_model = NoInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, a = _bound(context)
        p = service.compute_progress(context.session, a)
        return ToolResult(ok=True, data={"target_count": p.target_count, "submitted_count": p.submitted_count,
                                         "late_count": p.late_count, "pending_count": len(p.pending_ids),
                                         "all_submitted": p.all_submitted})


class GetPendingStudents(_GuardianTool):
    name: ClassVar[str] = "get_pending_students"
    description: ClassVar[str] = ("Students who have not submitted (student_id only) with their follow-up attempts "
                                  "and whether one is already active.")
    input_model = LimitInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, a = _bound(context)
        p = service.compute_progress(context.session, a)
        shown = p.pending_ids[:cast(LimitInput, args).limit]
        states = service.followup_states(context.session, a.id, shown)
        return ToolResult(ok=True, data={"pending_count": len(p.pending_ids), "pending": [
            {"student_id": sid, "attempts": states[sid].attempts, "active_followup": states[sid].active,
             "last_requested_at": _iso(states[sid].last_requested_at)} for sid in shown]})


class GetRecentAssignmentEvents(_GuardianTool):
    name: ClassVar[str] = "get_recent_assignment_events"
    description: ClassVar[str] = "Most recent submissions and follow-up requests for this assignment (ids and statuses)."
    input_model = LimitInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, a = _bound(context)
        limit, session = cast(LimitInput, args).limit, context.session
        submissions = session.execute(select(AssignmentSubmission.student_id, AssignmentSubmission.status,
                                             AssignmentSubmission.submitted_at)
                                      .where(AssignmentSubmission.assignment_id == a.id)
                                      .order_by(AssignmentSubmission.submitted_at.desc()).limit(limit)).all()
        followups = session.execute(select(AssignmentFollowup.student_id, AssignmentFollowup.status,
                                           AssignmentFollowup.attempt_number, AssignmentFollowup.created_at)
                                    .where(AssignmentFollowup.assignment_id == a.id)
                                    .order_by(AssignmentFollowup.id.desc()).limit(limit)).all()
        return ToolResult(ok=True, data={
            "submissions": [{"student_id": s, "status": st.value, "at": _iso(at)} for s, st, at in submissions],
            "followups": [{"student_id": s, "status": st.value, "attempt": n, "at": _iso(at)} for s, st, n, at in followups]})


class GetStudentFollowupState(_GuardianTool):
    name: ClassVar[str] = "get_student_followup_state"
    description: ClassVar[str] = ("One targeted student's follow-up state and whether policy would allow a follow-up "
                                  "now (with the reason code).")
    input_model = StudentInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, a = _bound(context)
        sid, session = cast(StudentInput, args).student_id, context.session
        is_target = session.execute(select(AssignmentTarget.id).where(
            AssignmentTarget.assignment_id == a.id, AssignmentTarget.student_id == sid)).first() is not None
        if not is_target:
            raise ToolExecutionError("NOT_A_TARGET")
        submitted = session.execute(select(AssignmentSubmission.id).where(
            AssignmentSubmission.assignment_id == a.id, AssignmentSubmission.student_id == sid)).first() is not None
        state = service.followup_states(session, a.id, [sid])[sid]
        decision = rules.evaluate_followup(
            now=context.now, assignment_status=a.status, published_at=a.published_at or context.now,
            deadline_at=a.deadline_at, is_target=True, has_submitted=submitted, active_followup=state.active,
            attempts_so_far=state.attempts, last_requested_at=state.last_requested_at, policy=self.policy)
        return ToolResult(ok=True, data={"student_id": sid, "submitted": submitted, "attempts": state.attempts,
                                         "max_attempts": self.policy.max_attempts, "active_followup": state.active,
                                         "last_requested_at": _iso(state.last_requested_at),
                                         "followup_allowed_now": decision.allowed, "reason": decision.reason})


class RequestStudentFollowup(_GuardianTool):
    """Internal request tool: asks for follow-ups; code decides each one. Writes only the declared models."""

    name: ClassVar[str] = "request_student_followup"
    description: ClassVar[str] = ("Request contact with pending students (no message is sent now). Each request is "
                                  "checked by institution policy; the result says requested or suppressed + reason.")
    input_model = FollowupRequestInput
    request_models = frozenset({AssignmentFollowup, DomainEvent, OperationAuditEvent})

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        mission, a = _bound(context)
        body = cast(FollowupRequestInput, args)
        results = service.request_followups(
            context.session, mission=mission, assignment=a, student_ids=body.student_ids, purpose=body.purpose,
            preferred_channel=body.preferred_channel, policy=self.policy, now=context.now,
            actor_account_id=context.account_id)
        return ToolResult(ok=True, data={
            "requested": sum(1 for r in results if r["result"] == "requested"),
            "suppressed": sum(1 for r in results if r["result"] == "suppressed"), "results": results})


TOOL_CLASSES = (GetAssignmentState, GetAssignmentProgress, GetPendingStudents, GetRecentAssignmentEvents,
                GetStudentFollowupState, RequestStudentFollowup)


# --- Supervisor ----------------------------------------------------------------------------------------------------


class GuardianSupervisor:
    def __init__(self, policy: rules.FollowupPolicy) -> None:
        self.policy = policy

    def _checkpoints(self, assignment: Assignment, now: datetime) -> List[datetime]:
        return rules.checkpoints(assignment.published_at or now, assignment.deadline_at, self.policy.checkpoint_hours)

    @staticmethod
    def _drain(session: Session, mission: AgentMission, now: datetime) -> int:
        """Consume every leftover event addressed to the mission: they only signal a wake; state comes from SQL."""
        events = list(session.execute(select(DomainEvent).where(
            DomainEvent.consumed_at.is_(None), DomainEvent.subject_type == SUBJECT_MISSION,
            DomainEvent.subject_id == str(mission.id))).scalars())
        for event in events:
            event.consumed_at = now
        return len(events)

    @staticmethod
    def _audit(session: Session, mission: AgentMission, assignment: Assignment, event_type: str, message: str,
               now: datetime, **metadata: Any) -> None:
        service._audit(session, event_type=event_type, actor_account_id=mission.owner_account_id,
                       actor_role=service.AGENT_ROLE, assignment_id=assignment.id, message=message, now=now,
                       mission_id=mission.id, **metadata)

    def _set_checkpoint(self, mission: AgentMission, wake: Optional[datetime]) -> None:
        mission.context = {**(mission.context or {}), CHECKPOINT_KEY: _iso(wake)}

    def review(self, session: Session, mission: AgentMission, now: datetime, trigger: Trigger) -> SupervisorVerdict:
        self._drain(session, mission, now)
        assignment = mission_assignment(session, mission)
        if assignment is None:
            return SupervisorVerdict("fail", code="ASSIGNMENT_NOT_FOUND", outcome={"result": "ASSIGNMENT_NOT_FOUND"})
        if rules.assignment_cancelled(assignment.status):
            service.close_active_followups(session, assignment.id, now=now)
            return SupervisorVerdict("cancel", code="ASSIGNMENT_CANCELLED",
                                     outcome={"result": "ASSIGNMENT_CANCELLED", "assignment_id": assignment.id})
        progress = service.compute_progress(session, assignment)
        counts = {"target_count": progress.target_count, "submitted_count": progress.submitted_count,
                  "late_count": progress.late_count, "pending_count": len(progress.pending_ids)}
        if progress.all_submitted:
            self._audit(session, mission, assignment, "ASSIGNMENT_COMPLETED",
                        f"Assignment {assignment.id}: every targeted student submitted (verified).", now, **counts)
            return SupervisorVerdict("complete", code="ALL_SUBMITTED",
                                     outcome={"result": "ALL_SUBMITTED", "assignment_id": assignment.id, **counts})
        if rules.deadline_passed(assignment.deadline_at, now) and now >= assignment.deadline_at + rules.DEADLINE_SETTLE:
            closed = service.close_active_followups(session, assignment.id, now=now)
            self._audit(session, mission, assignment, "ASSIGNMENT_DEADLINE_REACHED",
                        f"Assignment {assignment.id}: deadline reached with {len(progress.pending_ids)} pending.", now,
                        followups_closed=closed, **counts)
            return SupervisorVerdict("fail", code="DEADLINE_REACHED_WITH_PENDING", outcome={
                "result": "DEADLINE_REACHED_WITH_PENDING", "assignment_id": assignment.id, **counts})

        points = self._checkpoints(assignment, now)
        nxt = rules.next_checkpoint(points, now)
        window_opens = rules.reminder_window_opens(assignment.published_at or now, assignment.deadline_at,
                                                   self.policy.checkpoint_hours)
        due = _parse((mission.context or {}).get(CHECKPOINT_KEY))
        early_wake = trigger in ("event", "wake") and due is not None and now < due
        if trigger != "running" and (early_wake or now < window_opens):
            # Between checkpoints, or before any follow-up could be allowed: nothing for the brain to decide.
            wake = due if early_wake else nxt
            self._set_checkpoint(mission, wake)
            return SupervisorVerdict("wait", waiting_for=DomainEventType.ASSIGNMENT_SUBMITTED, wake_at=wake)
        return SupervisorVerdict("continue", state={
            "assignment_id": assignment.id, "deadline_at": _iso(assignment.deadline_at), "now": _iso(now),
            "hours_to_deadline": round((assignment.deadline_at - now).total_seconds() / 3600, 2),
            "followup_window_open": now >= window_opens, "next_checkpoint": _iso(nxt), **counts})

    def plan_wait(self, session: Session, mission: AgentMission, requested_wake: Optional[datetime],
                  now: datetime) -> Tuple[DomainEventType, Optional[datetime]]:
        assignment = mission_assignment(session, mission)
        wake = rules.next_checkpoint(self._checkpoints(assignment, now), now) if assignment is not None else None
        if requested_wake is not None and wake is not None and now + MIN_WAKE <= requested_wake < wake:
            wake = requested_wake
        pending_events = session.execute(select(DomainEvent.id).where(
            DomainEvent.consumed_at.is_(None), DomainEvent.subject_type == SUBJECT_MISSION,
            DomainEvent.subject_id == str(mission.id))).first()
        self._set_checkpoint(mission, wake)
        if pending_events is not None:  # an event arrived during this run: look again right away (no AI call)
            wake = now
        return DomainEventType.ASSIGNMENT_SUBMITTED, wake


def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


# --- Registration --------------------------------------------------------------------------------------------------


def guardian_spec(policy: rules.FollowupPolicy) -> AgentSpec:
    return AgentSpec(
        GUARDIAN_AGENT_KEY, "Monitors one published assignment until every targeted student submits or the deadline.",
        allowed_tools=frozenset(t.name for t in TOOL_CLASSES),
        allowed_delegate_agents=frozenset({COMMUNICATION_AGENT_KEY}), supported_roles=GUARDIAN_ROLES,
        allowed_decisions=frozenset({DecisionKind.TOOL, DecisionKind.WAIT, DecisionKind.DELEGATE, DecisionKind.COMPLETE,
                                     DecisionKind.FAIL, DecisionKind.REPLAN}),
        wait_events=frozenset({DomainEventType.ASSIGNMENT_SUBMITTED}),
        accepts_delegation=False, user_creatable=False, autonomous=True, supervisor=GuardianSupervisor(policy),
    )


def register_assignment_guardian(agents: AgentRegistry, tools: ToolRegistry,
                                 policy: Optional[rules.FollowupPolicy] = None) -> None:
    policy = policy or policy_from_env()
    for tool_class in TOOL_CLASSES:
        tools.register(tool_class(policy))
    agents.register(guardian_spec(policy))
