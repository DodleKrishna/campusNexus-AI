"""Exam Guardian (AgentOS V2 Phase 4): a persistent AgentRuntime agent per scheduled exam.

Created only by scheduling an exam (``app.services.exams.schedule``; ``user_creatable=False``) and advanced by the
due-mission worker (``autonomous=True``). Every tool is bound to the mission's own exam
(``exam.guardian_mission_id == mission.id``): no input can name another exam, and no output carries a name, phone
number or e-mail address.

Division of labour (cost control), as in the Assignment Guardian:

* ``ExamSupervisor`` (code) decides everything SQL can: times (reminder checkpoints, start grace, end, absence
  re-checks, the terminal deadline), the target count, attendance state, success (exam finished AND every target
  present / exempt / makeup-completed), cancellation, and the terminal deadline (FAILED with outcome
  ``UNRESOLVED_AT_TERMINAL_DEADLINE`` -- never a success). An attendance event between checkpoints is handled
  without any AI call, and so is any checkpoint at which policy would allow no follow-up at all.
* The AgentBrain is asked only when a follow-up could be allowed: what to inspect, whether to request a permitted
  reminder / absence follow-up, whether to replan. ``request_exam_followup`` is an internal request tool: every
  requested student is decided by ``evaluate_exam_followup`` and only an allowed one becomes an ``ExamFollowup`` +
  COMMUNICATION_REQUESTED event. Nothing is sent here (``app.communication``).
* COMPLETE/FAIL are decided by the supervisor only (a brain COMPLETE/FAIL is re-checked, never trusted).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agentos import policy_env
from app.agentos.events import SUBJECT_MISSION
from app.agentos.registry import AgentRegistry, AgentSpec, AgentTool, ToolContext, ToolExecutionError, ToolRegistry, ToolResult
from app.agentos.schemas import DecisionKind, DomainEventType
from app.agentos.supervisor import SupervisorVerdict, Trigger
from app.db.models.academic import Exam, ExamStatus
from app.db.models.agent_kernel import AgentMission, DomainEvent
from app.db.models.exam import ExamAttendance, ExamAttendanceStatus, ExamFollowup
from app.db.models.workflow import OperationAuditEvent
from app.rules import exam_rules as rules
from app.rules.followup_policy import FollowupPolicy
from app.schemas.enums import UserRole
from app.services import exams as service
from app.services.class_schedule import CAMPUS_TZ

GUARDIAN_AGENT_KEY = service.GUARDIAN_AGENT_KEY
GUARDIAN_ROLES = frozenset({UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN})
MIN_WAKE = timedelta(minutes=15)
CHECKPOINT_KEY = "guardian_checkpoint"
WAIT_EVENTS = frozenset({DomainEventType.EXAM_ATTENDANCE_MARKED, DomainEventType.MAKEUP_EXAM_COMPLETED,
                         DomainEventType.EXAM_STARTED, DomainEventType.EXAM_ENDED, DomainEventType.EXAM_CANCELLED})

ENV_REMINDER_HOURS = "CAMPUSNEXUS_EXAM_REMINDER_HOURS"  # "24,2,0.5"
ENV_GRACE = "CAMPUSNEXUS_EXAM_START_GRACE_MINUTES"  # 15
ENV_COOLDOWN = "CAMPUSNEXUS_EXAM_FOLLOWUP_COOLDOWN_MINUTES"  # 360
ENV_MAX_ATTEMPTS = "CAMPUSNEXUS_EXAM_FOLLOWUP_MAX_ATTEMPTS"  # 3
ENV_TERMINAL_DAYS = "CAMPUSNEXUS_EXAM_TERMINAL_DAYS"  # 7 (when the exam has no makeup deadline)


def policy_from_env() -> rules.ExamPolicy:
    quiet_start, quiet_end = policy_env.quiet_hours()
    hours = policy_env.hours_list(ENV_REMINDER_HOURS, "24,2,0.5")
    reminder = FollowupPolicy(cooldown=timedelta(minutes=20), max_attempts=max(1, len(hours)), quiet_start=quiet_start,
                              quiet_end=quiet_end, timezone=CAMPUS_TZ)
    absence = FollowupPolicy(cooldown=timedelta(minutes=policy_env.number(ENV_COOLDOWN, 360)),
                             max_attempts=int(policy_env.number(ENV_MAX_ATTEMPTS, 3)), quiet_start=quiet_start,
                             quiet_end=quiet_end, timezone=CAMPUS_TZ)
    return rules.ExamPolicy(reminder=reminder, absence=absence, reminder_hours=hours,
                            start_grace=timedelta(minutes=policy_env.number(ENV_GRACE, 15)),
                            terminal_after_end=timedelta(days=policy_env.number(ENV_TERMINAL_DAYS, 7)))


def mission_exam(session: Session, mission: Optional[AgentMission]) -> Optional[Exam]:
    """The exam this Guardian mission was created for -- and only if it points back at the mission."""
    if mission is None or mission.agent_key != GUARDIAN_AGENT_KEY:
        return None
    exam_id = ((mission.context or {}).get("inputs") or {}).get("exam_id")
    if not isinstance(exam_id, int) or isinstance(exam_id, bool):
        return None
    exam = session.get(Exam, exam_id)
    return exam if exam is not None and exam.guardian_mission_id == mission.id else None


def _bound(context: ToolContext) -> Tuple[AgentMission, Exam]:
    mission = context.session.get(AgentMission, context.mission_id)
    exam = mission_exam(context.session, mission)
    if exam is None:
        raise ToolExecutionError("EXAM_NOT_BOUND")
    return cast(AgentMission, mission), exam


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def phase(exam: Exam, now: datetime, policy: rules.ExamPolicy) -> str:
    if now < exam.scheduled_start:
        return "before_exam"
    return "start_grace" if now < rules.grace_ends(exam.scheduled_start, policy) else "after_start"


def purpose_for(current_phase: str) -> Optional[str]:
    return {"before_exam": rules.PURPOSE_REMINDER, "after_start": rules.PURPOSE_ABSENCE}.get(current_phase)


def allowed_now(session: Session, exam: Exam, progress: service.ExamProgress, purpose: Optional[str],
                policy: rules.ExamPolicy, now: datetime) -> int:
    """How many follow-ups of ``purpose`` policy would allow right now (a read-only preview; nothing is written)."""
    if purpose is None:
        return 0
    candidates = progress.target_ids if purpose == rules.PURPOSE_REMINDER else progress.ids_with(ExamAttendanceStatus.ABSENT)
    states = service.followup_states(session, exam.id, candidates, purpose)
    terminal = service.terminal_deadline(exam, policy)
    return sum(1 for sid in candidates if rules.evaluate_exam_followup(
        now=now, purpose=purpose, exam_status=exam.status, start=exam.scheduled_start, terminal=terminal, is_target=True,
        attendance=progress.attendance.get(sid), active_followup=states[sid].active, attempts_so_far=states[sid].attempts,
        last_requested_at=states[sid].last_requested_at, policy=policy).allowed)


# --- Tools (compact outputs: ids, counts, statuses; never names or contact details) -------------------------------


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LimitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=50)


Purpose = Literal["exam_reminder", "absence_followup"]


class FollowupStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_id: int = Field(ge=1)
    purpose: Purpose = "absence_followup"


class ExamFollowupRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Purpose
    # None: every target (reminder) / every confirmed absentee (absence follow-up); code still decides each one.
    student_ids: Optional[List[int]] = Field(default=None, min_length=1, max_length=50)
    preferred_channel: Optional[Literal["in_app", "sms", "voice_call"]] = None


class _GuardianTool(AgentTool):
    required_roles = GUARDIAN_ROLES

    def __init__(self, policy: rules.ExamPolicy) -> None:
        self.policy = policy


class GetExamState(_GuardianTool):
    name: ClassVar[str] = "get_exam_state"
    description: ClassVar[str] = ("This mission's exam: status, start/end, phase, whether it finished, the reminder "
                                  "window, the end of the start grace period, the terminal deadline, next checkpoint.")
    input_model = NoInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, e = _bound(context)
        terminal = service.terminal_deadline(e, self.policy)
        points = rules.checkpoints(e.published_at or context.now, e.scheduled_start, e.scheduled_end, terminal, self.policy)
        return ToolResult(ok=True, data={
            "exam_id": e.id, "status": e.status.value if e.status else None, "exam_type": e.exam_type,
            "starts_at": _iso(e.scheduled_start), "ends_at": _iso(e.scheduled_end),
            "phase": phase(e, context.now, self.policy),
            "finished": rules.finished(e.status, e.scheduled_end, context.now),
            "reminder_window_opens": _iso(rules.reminder_window_opens(e.scheduled_start, self.policy)),
            "start_grace_ends": _iso(rules.grace_ends(e.scheduled_start, self.policy)),
            "terminal_deadline": _iso(terminal), "next_checkpoint": _iso(rules.next_checkpoint(points, context.now))})


class GetExamProgress(_GuardianTool):
    name: ClassVar[str] = "get_exam_progress"
    description: ClassVar[str] = "Counts: targets, present, absent, exempt, makeup completed, unmarked; all resolved?"
    input_model = NoInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, e = _bound(context)
        p = service.compute_progress(context.session, e)
        return ToolResult(ok=True, data={**p.counts(), "all_resolved": p.all_resolved})


class GetExamAbsentees(_GuardianTool):
    name: ClassVar[str] = "get_exam_absentees"
    description: ClassVar[str] = ("Students confirmed ABSENT (student_id only) with their absence follow-up attempts and "
                                  "whether one is active; plus the count still unmarked.")
    input_model = LimitInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, e = _bound(context)
        p = service.compute_progress(context.session, e)
        absent = p.ids_with(ExamAttendanceStatus.ABSENT)
        shown = absent[:cast(LimitInput, args).limit]
        states = service.followup_states(context.session, e.id, shown, rules.PURPOSE_ABSENCE)
        return ToolResult(ok=True, data={"absent_count": len(absent), "unmarked_count": len(p.unmarked_ids), "absent": [
            {"student_id": sid, "attempts": states[sid].attempts, "active_followup": states[sid].active,
             "last_requested_at": _iso(states[sid].last_requested_at)} for sid in shown]})


class GetExamFollowupState(_GuardianTool):
    name: ClassVar[str] = "get_exam_followup_state"
    description: ClassVar[str] = ("One targeted student's attendance, follow-up attempts for a purpose and whether policy "
                                  "would allow one now (with the reason code).")
    input_model = FollowupStateInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, e = _bound(context)
        body = cast(FollowupStateInput, args)
        p = service.compute_progress(context.session, e)
        if body.student_id not in p.target_ids:
            raise ToolExecutionError("NOT_A_TARGET")
        state = service.followup_states(context.session, e.id, [body.student_id], body.purpose)[body.student_id]
        decision = rules.evaluate_exam_followup(
            now=context.now, purpose=body.purpose, exam_status=e.status, start=e.scheduled_start,
            terminal=service.terminal_deadline(e, self.policy), is_target=True, attendance=p.attendance.get(body.student_id),
            active_followup=state.active, attempts_so_far=state.attempts, last_requested_at=state.last_requested_at,
            policy=self.policy)
        attendance = p.attendance.get(body.student_id)
        return ToolResult(ok=True, data={
            "student_id": body.student_id, "attendance": attendance.value if attendance else None, "purpose": body.purpose,
            "attempts": state.attempts, "active_followup": state.active, "last_requested_at": _iso(state.last_requested_at),
            "followup_allowed_now": decision.allowed, "reason": decision.reason})


class GetRecentExamEvents(_GuardianTool):
    name: ClassVar[str] = "get_recent_exam_events"
    description: ClassVar[str] = "Most recent attendance changes and follow-up requests for this exam (ids and statuses)."
    input_model = LimitInput

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        _, e = _bound(context)
        limit, session = cast(LimitInput, args).limit, context.session
        marks = session.execute(select(ExamAttendance.student_id, ExamAttendance.status, ExamAttendance.marked_at)
                                .where(ExamAttendance.exam_id == e.id)
                                .order_by(ExamAttendance.marked_at.desc()).limit(limit)).all()
        followups = session.execute(select(ExamFollowup.student_id, ExamFollowup.purpose, ExamFollowup.status,
                                           ExamFollowup.attempt_number, ExamFollowup.created_at)
                                    .where(ExamFollowup.exam_id == e.id).order_by(ExamFollowup.id.desc()).limit(limit)).all()
        return ToolResult(ok=True, data={
            "attendance": [{"student_id": s, "status": st.value, "at": _iso(at)} for s, st, at in marks],
            "followups": [{"student_id": s, "purpose": pu, "status": st.value, "attempt": n, "at": _iso(at)}
                          for s, pu, st, n, at in followups]})


class RequestExamFollowup(_GuardianTool):
    """Internal request tool: asks for follow-ups; code decides each one. Writes only the declared models."""

    name: ClassVar[str] = "request_exam_followup"
    description: ClassVar[str] = ("Request a pre-exam reminder (exam_reminder) or contact with confirmed absentees "
                                  "(absence_followup). No message is sent now. Each student is checked by institution "
                                  "policy; the result says requested or suppressed + reason. Omit student_ids for all "
                                  "eligible students.")
    input_model = ExamFollowupRequestInput
    request_models = frozenset({ExamFollowup, DomainEvent, OperationAuditEvent})

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        mission, e = _bound(context)
        body = cast(ExamFollowupRequestInput, args)
        results = service.request_followups(
            context.session, mission=mission, exam=e, student_ids=body.student_ids, purpose=body.purpose,
            preferred_channel=body.preferred_channel, policy=self.policy, now=context.now,
            actor_account_id=context.account_id)
        return ToolResult(ok=True, data={
            "purpose": body.purpose, "requested": sum(1 for r in results if r["result"] == "requested"),
            "suppressed": sum(1 for r in results if r["result"] == "suppressed"), "results": results[:50]})


TOOL_CLASSES = (GetExamState, GetExamProgress, GetExamAbsentees, GetExamFollowupState, GetRecentExamEvents,
                RequestExamFollowup)


# --- Supervisor ----------------------------------------------------------------------------------------------------


class ExamSupervisor:
    def __init__(self, policy: rules.ExamPolicy) -> None:
        self.policy = policy

    def _checkpoints(self, exam: Exam, now: datetime) -> List[datetime]:
        return rules.checkpoints(exam.published_at or now, exam.scheduled_start, exam.scheduled_end,
                                 service.terminal_deadline(exam, self.policy), self.policy)

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
    def _audit(session: Session, mission: AgentMission, exam: Exam, event_type: str, message: str, now: datetime,
               **metadata: Any) -> None:
        service._audit(session, event_type=event_type, actor_account_id=mission.owner_account_id,
                       actor_role=service.AGENT_ROLE, exam_id=exam.id, message=message, now=now, mission_id=mission.id,
                       **metadata)

    def _set_checkpoint(self, mission: AgentMission, wake: Optional[datetime]) -> None:
        mission.context = {**(mission.context or {}), CHECKPOINT_KEY: _iso(wake)}

    def _wait(self, mission: AgentMission, wake: Optional[datetime]) -> SupervisorVerdict:
        self._set_checkpoint(mission, wake)
        return SupervisorVerdict("wait", waiting_for=DomainEventType.EXAM_ATTENDANCE_MARKED, wake_at=wake)

    def review(self, session: Session, mission: AgentMission, now: datetime, trigger: Trigger) -> SupervisorVerdict:
        self._drain(session, mission, now)
        exam = mission_exam(session, mission)
        if exam is None:
            return SupervisorVerdict("fail", code="EXAM_NOT_FOUND", outcome={"result": "EXAM_NOT_FOUND"})
        if exam.status == ExamStatus.CANCELLED:
            service.close_active_followups(session, exam.id, now=now)
            return SupervisorVerdict("cancel", code="EXAM_CANCELLED", outcome={"result": "EXAM_CANCELLED", "exam_id": exam.id})
        progress = service.compute_progress(session, exam)
        counts = progress.counts()
        terminal = service.terminal_deadline(exam, self.policy)
        is_finished = rules.finished(exam.status, exam.scheduled_end, now)
        if is_finished and progress.all_resolved:
            closed = service.close_active_followups(session, exam.id, now=now)
            self._audit(session, mission, exam, "EXAM_COMPLETED",
                        f"Exam {exam.id}: finished and every target resolved (verified).", now, followups_closed=closed,
                        **counts)
            return SupervisorVerdict("complete", code="ALL_RESOLVED", outcome={"result": "ALL_RESOLVED", "exam_id": exam.id,
                                                                                **counts})
        if now >= terminal + rules.SETTLE:
            closed = service.close_active_followups(session, exam.id, now=now)
            self._audit(session, mission, exam, "EXAM_UNRESOLVED",
                        f"Exam {exam.id}: terminal deadline reached with {len(progress.unresolved_ids)} unresolved.", now,
                        followups_closed=closed, unresolved_count=len(progress.unresolved_ids), **counts)
            return SupervisorVerdict("fail", code="UNRESOLVED_AT_TERMINAL_DEADLINE", outcome={
                "result": "UNRESOLVED_AT_TERMINAL_DEADLINE", "exam_id": exam.id,
                "unresolved_count": len(progress.unresolved_ids), **counts})

        nxt = rules.next_checkpoint(self._checkpoints(exam, now), now)
        current = phase(exam, now, self.policy)
        purpose = purpose_for(current)
        allowed = allowed_now(session, exam, progress, purpose, self.policy, now)
        due = _parse((mission.context or {}).get(CHECKPOINT_KEY))
        early = trigger in ("event", "wake") and due is not None and now < due
        if trigger != "running":
            # No AI call when no follow-up could be allowed, or (for reminders) between checkpoints. A newly confirmed
            # absentee who may be followed up is worth a decision right away.
            if allowed == 0 or (early and purpose != rules.PURPOSE_ABSENCE):
                return self._wait(mission, due if early else nxt)
        return SupervisorVerdict("continue", state={
            "exam_id": exam.id, "now": _iso(now), "phase": current, "followup_purpose": purpose,
            "followups_allowed_now": allowed, "starts_at": _iso(exam.scheduled_start), "ends_at": _iso(exam.scheduled_end),
            "finished": is_finished, "terminal_deadline": _iso(terminal), "next_checkpoint": _iso(nxt), **counts})

    def plan_wait(self, session: Session, mission: AgentMission, requested_wake: Optional[datetime],
                  now: datetime) -> Tuple[DomainEventType, Optional[datetime]]:
        exam = mission_exam(session, mission)
        wake = rules.next_checkpoint(self._checkpoints(exam, now), now) if exam is not None else None
        if requested_wake is not None and wake is not None and now + MIN_WAKE <= requested_wake < wake:
            wake = requested_wake
        pending_events = session.execute(select(DomainEvent.id).where(
            DomainEvent.consumed_at.is_(None), DomainEvent.subject_type == SUBJECT_MISSION,
            DomainEvent.subject_id == str(mission.id))).first()
        self._set_checkpoint(mission, wake)
        if pending_events is not None:  # an event arrived during this run: look again right away
            wake = now
        return DomainEventType.EXAM_ATTENDANCE_MARKED, wake


def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


# --- Registration --------------------------------------------------------------------------------------------------


def guardian_spec(policy: rules.ExamPolicy) -> AgentSpec:
    return AgentSpec(
        GUARDIAN_AGENT_KEY, "Monitors one scheduled exam until every target sat it, is exempt or completed a makeup.",
        allowed_tools=frozenset(t.name for t in TOOL_CLASSES), supported_roles=GUARDIAN_ROLES,
        allowed_decisions=frozenset({DecisionKind.TOOL, DecisionKind.WAIT, DecisionKind.COMPLETE, DecisionKind.FAIL,
                                     DecisionKind.REPLAN}),
        wait_events=WAIT_EVENTS, accepts_delegation=False, user_creatable=False, autonomous=True,
        supervisor=ExamSupervisor(policy),
    )


def register_exam_guardian(agents: AgentRegistry, tools: ToolRegistry, policy: Optional[rules.ExamPolicy] = None) -> None:
    policy = policy or policy_from_env()
    for tool_class in TOOL_CLASSES:
        tools.register(tool_class(policy))
    agents.register(guardian_spec(policy))
