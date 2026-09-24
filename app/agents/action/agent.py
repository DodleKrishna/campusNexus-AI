"""The Action Agent (CLAUDE.md component 7) -- Phase 7.

The only specialist agent permitted to call the Tool Gateway
(``app/tools/registry.py``). Conceptually mirrors the read-only agents'
template (``AgentMessage -> ... -> AgentResult`` + verifier), but its
``handle()`` is dispatched **twice** per approved action, exactly like any
other ``SpecialistAgent`` -- no new orchestrator concept is needed:

1. **First dispatch** (no approval exists yet for this mission step): build
   an ``ActionProposal`` from the plan's ``constraints`` + verified upstream
   facts, run the pre-action ``ActionVerifier`` check, and -- if it doesn't
   fail outright -- persist a PENDING ``ToolCallRecord``/``ApprovalRecord``
   and return ``VerificationStatus.NEEDS_REVIEW``. The existing scheduler
   turns that into ``TaskStatus.BLOCKED``/``MissionStatus.NEEDS_APPROVAL``
   and the mission run ends paused, unchanged from how a NEEDS_REVIEW
   specialist result already pauses a mission (Phase 5/6).
2. **Second dispatch** (an ``ApprovalRecord`` now exists with
   ``status=APPROVED``, via ``app/services/approval_gate.py`` + a fresh
   ``MissionOrchestrator.resume_mission`` call): rechecks preconditions
   against current DB state, executes via the Tool Gateway inside a
   transaction, independently re-reads the DB for the post-action check, and
   returns VERIFIED/FAILED.

A precheck that fails outright never creates an approval -- CLAUDE.md:
"never accept an unverified specialist recommendation as authorization to
execute." Phase 10: for ``register_event`` the propose-time schedule check is
built from the verified upstream Academic timetable/exam facts the planner
wires in as dependencies (``_upstream_schedule_check``), and a conflict found
there is a hard failure -- a known clash never reaches the Approval Gate.
The execute-time recheck still re-derives everything from current DB state.

A REJECTED approval is handled defensively here too (the scheduler should
never re-dispatch a FAILED step, but this never executes regardless).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.repositories.calendar import get_calendar_entry, get_student_calendar
from app.db.repositories.cases import get_case
from app.db.repositories.events import get_registration
from app.db.repositories.missions import (
    count_approvals_for_step,
    get_latest_approval_for_step,
    get_tool_call_by_id,
)
from app.db.repositories.students import get_student_by_id
from app.rules.action_preconditions import (
    CATEGORY_DEPARTMENTS,
    check_calendar_creation,
    check_case_creation,
    check_event_registration,
)
from app.rules.event_availability import find_exam_conflicts, find_timetable_conflicts
from app.schemas.action import ActionProposal
from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.enums import (
    AgentName,
    AgentResultStatus,
    ApprovalStatus,
    ToolAccessType,
    ToolExecutionStatus,
    UserRole,
    VerificationPhase,
    VerificationStatus,
)
from app.schemas.academic import ExamEntry, TimetableEntry
from app.schemas.events import EventSummary, ExamConflict, TimetableConflict
from app.schemas.tools import ToolCall
from app.schemas.verification import VerificationCheck, VerificationResult
from app.services import academic as academic_service
from app.services import events as events_service
from app.services.approval_gate import ApprovalGate
from app.services.context import ContextService
from app.services.knowledge import KnowledgeService
from app.tools.registry import ToolGateway
from app.verification.action import ActionVerifier

_ACTION_POLICY_QUERY = "registration cancellation calendar complaint grievance procedure policy"


def _resolve_now(as_of_raw: object) -> datetime:
    if as_of_raw:
        d = date.fromisoformat(str(as_of_raw))
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _new_verification(mission_id: str, task_id: str, phase: VerificationPhase, status: VerificationStatus, issues: List[str]) -> VerificationResult:
    return VerificationResult(
        verification_id=f"ver-{uuid.uuid4().hex[:12]}",
        mission_id=mission_id,
        task_id=task_id,
        phase=phase,
        status=status,
        issues=issues,
    )


# Phase 10: a schedule conflict that is actually *found* is a hard failure at
# both propose time (never shown to a human as approvable) and execute time.
# Only "couldn't check" (schedule_conflict_checked) stays soft.
_SCHEDULE_CONFLICT_BLOCKING_CHECKS = frozenset({"no_schedule_conflicts"})


@dataclass
class _UpstreamScheduleCheck:
    """Result of the propose-time schedule-conflict check, and where its input came from."""

    performed: bool = False
    source: str = "none"
    timetable_conflicts: List[TimetableConflict] = field(default_factory=list)
    exam_conflicts: List[ExamConflict] = field(default_factory=list)
    timetable_entries: Optional[int] = None
    exam_entries: Optional[int] = None

    def as_facts(self) -> Dict[str, object]:
        return {
            "performed": self.performed,
            "source": self.source,
            "timetable_entries_checked": self.timetable_entries,
            "exam_entries_checked": self.exam_entries,
            "timetable_conflicts": [c.model_dump(mode="json") for c in self.timetable_conflicts],
            "exam_conflicts": [c.model_dump(mode="json") for c in self.exam_conflicts],
        }


def _upstream_schedule_check(facts: Dict[str, object], event: EventSummary) -> _UpstreamScheduleCheck:
    """Propose-time conflict check, built *only* from verified upstream task facts.

    The dispatcher merges a task's dependencies' facts into its message, and
    a task is only dispatched once every dependency is COMPLETED (verified),
    so these facts come from verified Academic/Events Agent outputs. This
    never fetches the student's timetable/exams itself: missing upstream
    context is reported as "not checked", never silently filled in. (The
    execute-time ``_recheck`` is different on purpose -- it re-reads current
    DB state because upstream facts may be stale by then.)
    """
    raw_timetable, raw_exams = facts.get("timetable"), facts.get("exams")
    full_scope = facts.get("timetable_scope", "all") == "all" and facts.get("exams_scope", "all") == "all"
    if raw_timetable is not None and raw_exams is not None and full_scope:
        timetable = [TimetableEntry.model_validate(item) for item in raw_timetable]
        exams = [ExamEntry.model_validate(item) for item in raw_exams]
        return _UpstreamScheduleCheck(
            performed=True,
            source="upstream_academic_tasks",
            timetable_conflicts=find_timetable_conflicts(event, timetable),
            exam_conflicts=find_exam_conflicts(event, exams),
            timetable_entries=len(timetable),
            exam_entries=len(exams),
        )
    for raw in facts.get("assessments") or []:
        if raw.get("event", {}).get("event_id") == event.event_id and raw.get("conflict_check_performed"):
            return _UpstreamScheduleCheck(
                performed=True,
                source="upstream_events_assessment",
                timetable_conflicts=[TimetableConflict.model_validate(c) for c in raw.get("timetable_conflicts", [])],
                exam_conflicts=[ExamConflict.model_validate(c) for c in raw.get("exam_conflicts", [])],
            )
    return _UpstreamScheduleCheck()


@dataclass
class ActionAgentOutcome:
    """Local convenience bundle -- structurally satisfies app.graph.results.AgentOutcome."""

    agent_result: AgentResult
    verification: VerificationResult
    response_text: str


class ActionAgent:
    def __init__(
        self,
        *,
        session: Session,
        knowledge_service: KnowledgeService,
        tool_gateway: ToolGateway,
        verifier: Optional[ActionVerifier] = None,
        approval_gate: Optional[ApprovalGate] = None,
        requested_by: str = "mission_orchestrator",
    ) -> None:
        self._session = session
        self._knowledge = knowledge_service
        self._tool_gateway = tool_gateway
        self._verifier = verifier or ActionVerifier()
        self._context = ContextService(session)
        self._approval_gate = approval_gate or ApprovalGate(session)
        self._requested_by = requested_by

    # ------------------------------------------------------------------
    # Dispatch entry point
    # ------------------------------------------------------------------

    def handle(self, message: AgentMessage) -> ActionAgentOutcome:
        approval = get_latest_approval_for_step(self._session, message.task_id)
        if approval is not None and approval.status == ApprovalStatus.APPROVED:
            return self._execute(message, approval)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            return self._still_pending_outcome(message, approval)
        if approval is not None and approval.status == ApprovalStatus.REJECTED:
            return self._rejected_outcome(message, approval)
        # None, or the latest is EDIT_REQUIRED (superseded) -> propose fresh.
        return self._propose(message)

    def verify_claimed_result(self, tool_name: str, arguments: Dict[str, object], result_data: Dict[str, object]) -> VerificationResult:
        """Independently re-read the DB to check whether a claimed tool result
        actually persisted -- the public entry point for the same post-action
        re-check ``_execute`` always runs, usable standalone (e.g. auditing an
        externally-reported result) without touching approval/dispatch state."""
        checks = self._post_checks(tool_name, arguments, result_data)
        return self._verifier.verify_post_action(mission_id="n/a", task_id="n/a", checks=checks)

    def propose_edit(
        self,
        mission_id: str,
        task_id: str,
        *,
        student_id: str,
        new_constraints: Dict[str, object],
        requested_by: str,
        reason: Optional[str] = None,
        as_of: Optional[str] = None,
    ) -> ActionAgentOutcome:
        """Explicit, out-of-band operation: an approved-but-not-yet-executed action's
        payload is being changed. Marks the current approval EDIT_REQUIRED and builds
        a *new* proposal from ``new_constraints`` -- never mutates the old one in place."""
        current = get_latest_approval_for_step(self._session, task_id)
        if current is None:
            raise ValueError(f"no existing approval for step_id={task_id!r} to edit")
        tool_call_record = get_tool_call_by_id(self._session, current.tool_call_id)
        if tool_call_record is not None and tool_call_record.status == ToolExecutionStatus.SUCCESS:
            raise ValueError("cannot edit an action that has already been executed")

        self._approval_gate.mark_superseded(current.approval_id, requested_by=requested_by, reason=reason)

        facts: Dict[str, object] = {"student_id": student_id}
        if as_of:
            facts["as_of"] = as_of
        message = AgentMessage(
            message_id=f"msg-{uuid.uuid4().hex[:12]}",
            mission_id=mission_id,
            task_id=task_id,
            source=AgentName.MISSION_ORCHESTRATOR,
            target=AgentName.ACTION_AGENT,
            objective="edited action proposal",
            facts=facts,
            constraints=new_constraints,
        )
        return self._propose(message)

    # ------------------------------------------------------------------
    # Propose (first dispatch)
    # ------------------------------------------------------------------

    def _propose(self, message: AgentMessage) -> ActionAgentOutcome:
        tool_name = str(message.constraints.get("tool_name") or "").strip()
        student_id = str(message.facts.get("student_id") or "").strip()
        now = _resolve_now(message.facts.get("as_of"))

        if tool_name == "register_event":
            return self._propose_register_event(message, student_id, now)
        if tool_name == "create_calendar_event":
            return self._propose_calendar(message, student_id, now)
        if tool_name == "create_campus_case":
            return self._propose_case(message, student_id, now)

        verification = _new_verification(
            message.mission_id, message.task_id, VerificationPhase.PRE_ACTION, VerificationStatus.FAILED,
            [f"Unknown or missing tool_name in plan constraints: {tool_name!r}."],
        )
        return self._failed_outcome(message, verification, facts={"tool_name": tool_name})

    def _propose_register_event(self, message: AgentMessage, student_id: str, now: datetime) -> ActionAgentOutcome:
        constraints = message.constraints
        student = get_student_by_id(self._session, student_id) if student_id else None
        student_exists = student is not None

        event = None
        if student_exists:
            event_id = constraints.get("event_id")
            event_title = constraints.get("event_title")
            if event_id is not None:
                event = events_service.get_event_summary(self._session, int(event_id))
            elif event_title:
                event = events_service.get_event_summary_by_title(self._session, str(event_title))

        already_registered = False
        confirmed_registrations = 0
        schedule = _UpstreamScheduleCheck()
        if student_exists and event is not None:
            already_registered = (
                events_service.get_student_registration_status(self._session, student_id, event.event_id) is not None
            )
            confirmed_registrations = events_service.get_registration_count(self._session, event.event_id)
            schedule = _upstream_schedule_check(message.facts, event)
        conflict_performed = schedule.performed
        timetable_conflicts, exam_conflicts = schedule.timetable_conflicts, schedule.exam_conflicts

        checks = check_event_registration(
            student_exists=student_exists,
            event=event,
            confirmed_registrations=confirmed_registrations,
            already_registered=already_registered,
            now=now,
            timetable_conflicts=timetable_conflicts,
            exam_conflicts=exam_conflicts,
            conflict_check_performed=conflict_performed,
        )
        evidence = (
            self._knowledge.search(_ACTION_POLICY_QUERY, as_of=now.date(), top_k=3, visibility="public")
            if student_exists
            else []
        )
        # A conflict that is already *known* at propose time blocks the
        # proposal outright (Phase 10) -- it is never shown to a human as an
        # approvable action. Only "couldn't check" stays soft (NEEDS_REVIEW).
        precheck = self._verifier.verify_pre_action(
            mission_id=message.mission_id, task_id=message.task_id, checks=checks, evidence=evidence,
            blocking_check_names=_SCHEDULE_CONFLICT_BLOCKING_CHECKS,
        )
        schedule_facts = schedule.as_facts()
        if precheck.status == VerificationStatus.FAILED:
            outcome = self._failed_outcome(
                message, precheck, facts={"tool_name": "register_event", "precheck_status": precheck.status.value, "schedule_check": schedule_facts}
            )
            conflicts = [f"class {c.course_code}" for c in timetable_conflicts]
            conflicts += [f"{c.course_code} {c.exam_type} exam" for c in exam_conflicts]
            if conflicts:
                outcome.response_text += (
                    f" '{event.title}' clashes with your {', '.join(conflicts)}, so it was not proposed for approval."
                )
            return outcome

        assert event is not None and student is not None  # guaranteed by precheck not FAILED
        parameters = {"student_id": student_id, "event_id": event.event_id}
        description = (
            f"Register {student.user.full_name} (student_id={student_id}) for "
            f"'{event.title}' on {event.start_at.isoformat()} at {event.location}."
        )
        return self._create_proposal_and_pause(
            message,
            tool_name="register_event",
            target_resource=f"event:{event.event_id}",
            parameters=parameters,
            description=description,
            precheck=precheck,
            evidence=evidence,
            supporting_facts={
                "confirmed_registrations": confirmed_registrations,
                "capacity": event.capacity,
                "schedule_check": schedule_facts,
            },
        )

    def _propose_calendar(self, message: AgentMessage, student_id: str, now: datetime) -> ActionAgentOutcome:
        constraints = message.constraints
        student = get_student_by_id(self._session, student_id) if student_id else None
        student_exists = student is not None

        title = str(constraints.get("title") or "").strip()
        source_event_title = constraints.get("source_event_title")
        explicit_source_type = constraints.get("source_type")
        source_type = str(explicit_source_type) if explicit_source_type else ("event" if source_event_title else "personal")
        lead_time_hours = float(constraints.get("lead_time_hours", 0) or 0)
        duration_hours = float(constraints.get("duration_hours", 1) or 1)

        referenced_event_exists: Optional[bool] = None
        start_at: Optional[datetime] = None
        end_at: Optional[datetime] = None
        source_id: Optional[int] = None

        if student_exists:
            if source_event_title:
                source_event = events_service.get_event_summary_by_title(self._session, str(source_event_title))
                referenced_event_exists = source_event is not None
                if source_event is not None:
                    start_at = source_event.start_at - timedelta(hours=lead_time_hours)
                    end_at = start_at + timedelta(hours=duration_hours)
                    source_id = source_event.event_id
            else:
                raw_start, raw_end = constraints.get("start_at"), constraints.get("end_at")
                if raw_start and raw_end:
                    start_at = datetime.fromisoformat(str(raw_start))
                    end_at = datetime.fromisoformat(str(raw_end))

        duplicate_entry_exists = False
        overlapping_entry_count = 0
        if student_exists and start_at is not None and end_at is not None:
            duplicate_entry_exists = get_calendar_entry(self._session, student_id, title, start_at) is not None
            for entry in get_student_calendar(self._session, student_id):
                if _intervals_overlap(start_at, end_at, entry.start_at, entry.end_at):
                    overlapping_entry_count += 1

        checks = check_calendar_creation(
            student_exists=student_exists,
            referenced_event_exists=referenced_event_exists,
            duplicate_entry_exists=duplicate_entry_exists,
            overlapping_entry_count=overlapping_entry_count,
        )
        if student_exists and (start_at is None or end_at is None):
            checks.append(
                VerificationCheck(name="valid_times", passed=False, detail="Could not determine start_at/end_at for this calendar entry.")
            )

        precheck = self._verifier.verify_pre_action(mission_id=message.mission_id, task_id=message.task_id, checks=checks, evidence=[])
        if precheck.status == VerificationStatus.FAILED:
            return self._failed_outcome(message, precheck, facts={"tool_name": "create_calendar_event"})

        assert student is not None and start_at is not None and end_at is not None
        parameters = {
            "student_id": student_id,
            "title": title,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "source_type": source_type,
            "source_id": source_id,
        }
        description = (
            f"Create a personal calendar entry '{title}' for {student.user.full_name} "
            f"from {start_at.isoformat()} to {end_at.isoformat()}."
        )
        return self._create_proposal_and_pause(
            message,
            tool_name="create_calendar_event",
            target_resource=f"calendar:{student_id}:{title}",
            parameters=parameters,
            description=description,
            precheck=precheck,
            evidence=[],
            supporting_facts={"overlapping_entry_count": overlapping_entry_count},
        )

    def _propose_case(self, message: AgentMessage, student_id: str, now: datetime) -> ActionAgentOutcome:
        constraints = message.constraints
        student = get_student_by_id(self._session, student_id) if student_id else None
        student_exists = student is not None

        category = str(constraints.get("category") or "").strip()
        description_text = str(constraints.get("description") or "").strip()
        priority = str(constraints.get("priority") or "normal").strip()
        department = CATEGORY_DEPARTMENTS.get(category, "")

        checks = check_case_creation(
            student_exists=student_exists,
            category=category,
            priority=priority,
            department=department,
            description_present=bool(description_text),
        )
        evidence = (
            self._knowledge.search(_ACTION_POLICY_QUERY, as_of=now.date(), top_k=3, visibility="public")
            if student_exists
            else []
        )
        precheck = self._verifier.verify_pre_action(
            mission_id=message.mission_id, task_id=message.task_id, checks=checks, evidence=evidence
        )
        if precheck.status == VerificationStatus.FAILED:
            return self._failed_outcome(message, precheck, facts={"tool_name": "create_campus_case"})

        assert student is not None
        parameters = {
            "student_id": student_id,
            "category": category,
            "description": description_text,
            "priority": priority,
            "department": department,
        }
        description = f"File a {priority}-priority {category} complaint for {student.user.full_name}: {description_text[:120]}"
        return self._create_proposal_and_pause(
            message,
            tool_name="create_campus_case",
            target_resource=f"case:{student_id}:{category}",
            parameters=parameters,
            description=description,
            precheck=precheck,
            evidence=evidence,
            supporting_facts={},
        )

    def _create_proposal_and_pause(
        self,
        message: AgentMessage,
        *,
        tool_name: str,
        target_resource: str,
        parameters: Dict[str, object],
        description: str,
        precheck: VerificationResult,
        evidence: list,
        supporting_facts: Dict[str, object],
    ) -> ActionAgentOutcome:
        mission_id, task_id = message.mission_id, message.task_id
        proposal_id = f"prop-{uuid.uuid4().hex[:12]}"
        tool_call_id = f"tc-{uuid.uuid4().hex[:12]}"
        # Versioned so ActionAgent.propose_edit's fresh proposal never collides
        # with (or silently dedups against) the superseded one's key.
        version = count_approvals_for_step(self._session, task_id) + 1
        idempotency_key = f"{mission_id}:{task_id}:{tool_name}:v{version}"
        student_id = str(parameters.get("student_id"))

        proposal = ActionProposal(
            proposal_id=proposal_id,
            mission_id=mission_id,
            task_id=task_id,
            tool_name=tool_name,
            target_resource=target_resource,
            student_id=student_id,
            parameters=parameters,
            supporting_facts=supporting_facts,
            evidence_refs=[e.evidence_id for e in evidence],
            description=description,
        )
        self._context.record_tool_call(
            tool_call_id=tool_call_id,
            mission_id=mission_id,
            step_id=task_id,
            tool_name=tool_name,
            read_or_write=ToolAccessType.WRITE,
            arguments=parameters,
            idempotency_key=idempotency_key,
            status=ToolExecutionStatus.PENDING,
        )
        self._approval_gate.request_approval(
            mission_id=mission_id, step_id=task_id, tool_call_id=tool_call_id, action_summary=description, requested_by=self._requested_by
        )

        verification = VerificationResult(
            verification_id=precheck.verification_id,
            mission_id=mission_id,
            task_id=task_id,
            phase=VerificationPhase.PRE_ACTION,
            status=VerificationStatus.NEEDS_REVIEW,
            checks=precheck.checks,
            issues=precheck.issues + ["Awaiting human approval before execution."],
            evidence_refs=precheck.evidence_refs,
        )
        facts = {
            "tool_name": tool_name,
            "proposal_id": proposal_id,
            "tool_call_id": tool_call_id,
            "proposal": proposal.model_dump(mode="json"),
            "awaiting_approval": True,
            # The deterministic pre-check's own verdict, kept separate from the
            # task's NEEDS_REVIEW (which only means "paused for a human").
            "precheck_status": precheck.status.value,
        }
        agent_result = AgentResult(
            mission_id=mission_id, task_id=task_id, agent=AgentName.ACTION_AGENT, status=AgentResultStatus.PARTIAL,
            facts=facts, evidence=evidence, errors=precheck.issues,
        )
        return ActionAgentOutcome(
            agent_result=agent_result,
            verification=verification,
            response_text=description + " This action is awaiting human approval before it will be executed.",
        )

    # ------------------------------------------------------------------
    # Pending / rejected (defensive re-dispatch guards)
    # ------------------------------------------------------------------

    def _still_pending_outcome(self, message: AgentMessage, approval) -> ActionAgentOutcome:
        tool_call_record = get_tool_call_by_id(self._session, approval.tool_call_id)
        tool_name = tool_call_record.tool_name if tool_call_record else "unknown"
        verification = _new_verification(
            message.mission_id, message.task_id, VerificationPhase.PRE_ACTION, VerificationStatus.NEEDS_REVIEW,
            ["Still awaiting human approval; a pending action never executes automatically."],
        )
        agent_result = AgentResult(
            mission_id=message.mission_id, task_id=message.task_id, agent=AgentName.ACTION_AGENT,
            status=AgentResultStatus.PARTIAL, facts={"tool_name": tool_name, "awaiting_approval": True},
        )
        return ActionAgentOutcome(agent_result=agent_result, verification=verification, response_text="This action is still awaiting human approval.")

    def _rejected_outcome(self, message: AgentMessage, approval) -> ActionAgentOutcome:
        verification = _new_verification(
            message.mission_id, message.task_id, VerificationPhase.PRE_ACTION, VerificationStatus.FAILED,
            [f"Action was rejected: {approval.decision_reason or 'no reason given'}."],
        )
        agent_result = AgentResult(
            mission_id=message.mission_id, task_id=message.task_id, agent=AgentName.ACTION_AGENT,
            status=AgentResultStatus.FAILED, facts={"awaiting_approval": False, "rejected": True}, errors=list(verification.issues),
        )
        return ActionAgentOutcome(agent_result=agent_result, verification=verification, response_text="This action was rejected and will not be executed.")

    def _failed_outcome(self, message: AgentMessage, verification: VerificationResult, *, facts: Optional[Dict] = None) -> ActionAgentOutcome:
        agent_result = AgentResult(
            mission_id=message.mission_id, task_id=message.task_id, agent=AgentName.ACTION_AGENT,
            status=AgentResultStatus.FAILED, facts=facts or {}, errors=list(verification.issues),
        )
        text = "This action could not be completed: " + ("; ".join(verification.issues) if verification.issues else "a precondition check failed.")
        return ActionAgentOutcome(agent_result=agent_result, verification=verification, response_text=text)

    # ------------------------------------------------------------------
    # Execute (second dispatch, after approval)
    # ------------------------------------------------------------------

    def _execute(self, message: AgentMessage, approval) -> ActionAgentOutcome:
        tool_call_record = get_tool_call_by_id(self._session, approval.tool_call_id)
        if tool_call_record is None:
            verification = _new_verification(
                message.mission_id, message.task_id, VerificationPhase.POST_ACTION, VerificationStatus.FAILED,
                ["Approved action has no associated tool call record (data inconsistency)."],
            )
            return self._failed_outcome(message, verification)

        arguments = dict(tool_call_record.arguments)
        student_id = str(arguments.get("student_id") or "")
        now = _resolve_now(message.facts.get("as_of"))

        if tool_call_record.status == ToolExecutionStatus.SUCCESS:
            # Idempotent re-dispatch (e.g. resume_mission invoked twice) -- never
            # re-execute; just independently re-verify the persisted state.
            post_checks = self._post_checks(tool_call_record.tool_name, arguments, tool_call_record.result_data or {})
            post = self._verifier.verify_post_action(mission_id=message.mission_id, task_id=message.task_id, checks=post_checks)
            return self._executed_outcome(message, tool_call_record, post, already_executed=True)

        precheck_checks = self._recheck(tool_call_record.tool_name, arguments, now)
        precheck = self._verifier.verify_pre_action(
            mission_id=message.mission_id, task_id=message.task_id, checks=precheck_checks,
            blocking_check_names=_SCHEDULE_CONFLICT_BLOCKING_CHECKS,
        )
        if precheck.status != VerificationStatus.VERIFIED:
            # Unlike propose-time (where NEEDS_REVIEW still proceeds to human
            # approval), at execute-time a human has *already* approved based
            # on what could now be stale information -- so anything short of
            # a clean recheck blocks execution here. A newly-appeared
            # schedule conflict or capacity/duplicate change since approval
            # is exactly what this catches.
            self._context.update_tool_call_record(
                tool_call_record.tool_call_id,
                status=ToolExecutionStatus.FAILED,
                error="preconditions are no longer valid (recheck): " + "; ".join(precheck.issues),
            )
            # Force a hard FAILED verification regardless of whether the
            # recheck itself came back FAILED or (soft) NEEDS_REVIEW -- at
            # execute-time there is no "pause and ask a human again" option
            # left (the approval was already spent), so a NEEDS_REVIEW
            # recheck must not leave the task BLOCKED with no pending
            # approval to ever resolve it. See tests/test_action_safety.py.
            blocked = _new_verification(
                message.mission_id, message.task_id, VerificationPhase.PRE_ACTION, VerificationStatus.FAILED,
                [f"Action blocked: preconditions are no longer valid ({'; '.join(precheck.issues)})."],
            )
            return self._failed_outcome(
                message, blocked,
                facts={"tool_name": tool_call_record.tool_name, "execution_recheck_status": precheck.status.value},
            )

        tool_call = ToolCall(
            tool_call_id=tool_call_record.tool_call_id,
            mission_id=message.mission_id,
            task_id=message.task_id,
            tool_name=tool_call_record.tool_name,
            arguments=arguments,
            read_or_write=ToolAccessType.WRITE,
            requires_approval=True,
            idempotency_key=tool_call_record.idempotency_key,
        )
        result = self._tool_gateway.execute(self._session, tool_call, caller_role=UserRole.STUDENT, caller_student_id=student_id)
        self._context.update_tool_call_record(
            tool_call_record.tool_call_id, status=result.status, result_data=result.data, error=result.error, executed_at=utc_now()
        )

        if result.status != ToolExecutionStatus.SUCCESS:
            verification = _new_verification(
                message.mission_id, message.task_id, VerificationPhase.POST_ACTION, VerificationStatus.FAILED,
                [result.error or "tool execution failed"],
            )
            return self._failed_outcome(message, verification, facts={"tool_name": tool_call_record.tool_name})

        post_checks = self._post_checks(tool_call_record.tool_name, arguments, result.data or {})
        post = self._verifier.verify_post_action(mission_id=message.mission_id, task_id=message.task_id, checks=post_checks)
        self._context.update_tool_call_record(
            tool_call_record.tool_call_id, status=result.status, postcondition_verified=(post.status == VerificationStatus.VERIFIED)
        )
        return self._executed_outcome(
            message, tool_call_record, post, already_executed=False, result_data=result.data,
            execution_recheck_status=precheck.status.value,
        )

    def _executed_outcome(
        self,
        message: AgentMessage,
        tool_call_record,
        post: VerificationResult,
        *,
        already_executed: bool,
        result_data: Optional[Dict] = None,
        execution_recheck_status: Optional[str] = None,
    ) -> ActionAgentOutcome:
        status_map = {
            VerificationStatus.VERIFIED: AgentResultStatus.SUCCESS,
            VerificationStatus.NEEDS_REVIEW: AgentResultStatus.PARTIAL,
            VerificationStatus.FAILED: AgentResultStatus.FAILED,
        }
        facts = {
            "tool_name": tool_call_record.tool_name,
            "tool_result": result_data if result_data is not None else tool_call_record.result_data,
            "already_executed": already_executed,
            "postcondition_verified": post.status == VerificationStatus.VERIFIED,
            # The execute-time recheck's verdict (None on an idempotent
            # re-dispatch, which never re-executes and so never rechecks).
            "execution_recheck_status": execution_recheck_status,
        }
        agent_result = AgentResult(
            mission_id=message.mission_id, task_id=message.task_id, agent=AgentName.ACTION_AGENT,
            status=status_map[post.status], facts=facts, errors=list(post.issues),
        )
        if post.status == VerificationStatus.VERIFIED:
            response = f"Action '{tool_call_record.tool_name}' executed and verified successfully."
        else:
            response = f"Action '{tool_call_record.tool_name}' executed, but postcondition verification did not fully pass: " + "; ".join(post.issues)
        return ActionAgentOutcome(agent_result=agent_result, verification=post, response_text=response)

    # ------------------------------------------------------------------
    # Recheck (immediately before commit) / postcheck (independent re-read)
    # ------------------------------------------------------------------

    def _recheck(self, tool_name: str, arguments: Dict, now: datetime) -> List[VerificationCheck]:
        if tool_name == "register_event":
            student = get_student_by_id(self._session, str(arguments["student_id"]))
            student_exists = student is not None
            event = events_service.get_event_summary(self._session, int(arguments["event_id"])) if student_exists else None
            already_registered = False
            confirmed = 0
            timetable_conflicts: List[TimetableConflict] = []
            exam_conflicts: List[ExamConflict] = []
            if student_exists and event is not None:
                already_registered = (
                    events_service.get_student_registration_status(self._session, str(arguments["student_id"]), event.event_id)
                    is not None
                )
                confirmed = events_service.get_registration_count(self._session, event.event_id)
                # Independently re-derived from the student's *current*
                # timetable/exams -- not the propose-time EventsAgent
                # assessment, which can be stale by the time a human actually
                # approves (CLAUDE.md post-condition/idempotency spirit: never
                # trust an earlier snapshot for a safety-relevant recheck).
                timetable = academic_service.get_timetable(self._session, str(arguments["student_id"]))
                exams = academic_service.get_exam_schedule(self._session, str(arguments["student_id"]))
                timetable_conflicts = find_timetable_conflicts(event, timetable)
                exam_conflicts = find_exam_conflicts(event, exams)
            return check_event_registration(
                student_exists=student_exists, event=event, confirmed_registrations=confirmed,
                already_registered=already_registered, now=now, conflict_check_performed=student_exists and event is not None,
                timetable_conflicts=timetable_conflicts, exam_conflicts=exam_conflicts,
            )
        if tool_name == "create_calendar_event":
            student = get_student_by_id(self._session, str(arguments["student_id"]))
            student_exists = student is not None
            start_at = datetime.fromisoformat(str(arguments["start_at"]))
            duplicate_entry_exists = (
                get_calendar_entry(self._session, str(arguments["student_id"]), str(arguments["title"]), start_at) is not None
                if student_exists
                else False
            )
            referenced_event_exists: Optional[bool] = None
            if arguments.get("source_id") is not None:
                referenced_event_exists = events_service.get_event_summary(self._session, int(arguments["source_id"])) is not None
            return check_calendar_creation(
                student_exists=student_exists, referenced_event_exists=referenced_event_exists,
                duplicate_entry_exists=duplicate_entry_exists, overlapping_entry_count=0,
            )
        if tool_name == "create_campus_case":
            student = get_student_by_id(self._session, str(arguments["student_id"]))
            student_exists = student is not None
            return check_case_creation(
                student_exists=student_exists, category=str(arguments["category"]), priority=str(arguments["priority"]),
                department=str(arguments["department"]), description_present=bool(arguments.get("description")),
            )
        return [VerificationCheck(name="tool_name_supported", passed=False, detail=f"Unknown tool_name {tool_name!r}.")]

    def _post_checks(self, tool_name: str, arguments: Dict, result_data: Dict) -> List[VerificationCheck]:
        if tool_name == "register_event":
            reg = get_registration(self._session, int(arguments["event_id"]), str(arguments["student_id"]))
            passed = reg is not None and reg.status.value == "confirmed"
            return [
                VerificationCheck(
                    name="registration_persisted", passed=passed,
                    detail=None if passed else "No confirmed EventRegistration row found for this student/event after execution.",
                )
            ]
        if tool_name == "create_calendar_event":
            start_at = datetime.fromisoformat(str(arguments["start_at"]))
            entry = get_calendar_entry(self._session, str(arguments["student_id"]), str(arguments["title"]), start_at)
            passed = entry is not None
            return [
                VerificationCheck(
                    name="calendar_entry_persisted", passed=passed,
                    detail=None if passed else "No matching CalendarEvent row found after execution.",
                )
            ]
        if tool_name == "create_campus_case":
            case_code = result_data.get("case_code")
            case = get_case(self._session, str(case_code)) if case_code else None
            passed = case is not None and case.student.student_code == arguments["student_id"] and case.status.value == "open"
            return [
                VerificationCheck(
                    name="case_persisted", passed=passed,
                    detail=None if passed else "No open CampusCase row found for this student/case_code after execution.",
                )
            ]
        return [VerificationCheck(name="tool_name_supported", passed=False, detail=f"Unknown tool_name {tool_name!r}.")]
