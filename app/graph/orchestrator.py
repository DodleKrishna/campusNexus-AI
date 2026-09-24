"""LangGraph-based Mission Orchestrator (CLAUDE.md component 1).

Turns a natural-language student goal into a validated ``MissionPlan`` and
executes it as a dependency-aware DAG against ``app.graph.registry``'s
specialist agents, persisting every stage through the existing Context
Service (app/services/context.py). See docs/ARCHITECTURE.md's "Mission
Orchestrator" section for the full lifecycle/checkpointing writeup.

Graph shape: ``load_context -> generate_plan -> validate_plan ->
schedule_ready_tasks -> dispatch_and_collect -> update_mission_state ->
(replan | schedule_ready_tasks | finalize)``. Every node opens its own
short-lived DB session via ``ContextService`` (committed per call already,
so there is no cross-node session state to manage) -- the one exception is
``dispatch_and_collect``, which hands each *concurrently* dispatched task
its own session (app/graph/dispatcher.py), since ``Session`` is not
thread-safe.

Phase 10 bounded replanning: every failed task gets a deterministic failure
fingerprint (app/graph/failures.py) recorded in its audit event. When every
failure of a round is an exact repeat of an earlier one (same task, target,
reasons and input facts), the mission stops as FAILED with a
``duplicate_failure_detected`` audit event instead of replanning again --
a changed failure or changed input still gets its replan.
"""
from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timezone
from typing import Dict, List, Mapping, Optional, Tuple

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import utc_now
from app.db.repositories.missions import get_latest_approval_for_step, get_tool_call_by_id
from app.db.session import upgrade_schema
from app.graph.checkpoint import build_checkpointer
from app.graph.dispatcher import MISSION_GOAL_KEY, build_task_input_facts, dispatch_ready_tasks
from app.graph.failures import all_failures_repeated, failure_fingerprint
from app.graph.planner import generate_plan
from app.graph.registry import AgentRegistry
from app.graph.scheduler import compute_ready_and_blocked, has_blocked_tasks, has_failed_tasks, is_mission_complete
from app.graph.selection import build_selection_continuation_plan, continuation_step_id
from app.graph.state import OrchestratorState
from app.graph.validator import validate_plan
from app.llm.base import LLMProvider, LLMTransientError
from app.rules.action_preconditions import resolve_target_provenance
from app.schemas.action import TargetProvenance
from app.schemas.agent import AgentResult
from app.schemas.enums import (
    AgentName,
    AgentResultStatus,
    ApprovalStatus,
    MissionStatus,
    TaskStatus,
    ToolExecutionStatus,
    UserRole,
    VerificationPhase,
    VerificationStatus,
)
from app.schemas.evidence import Evidence
from app.schemas.mission import MissionPlan, MissionTask
from app.schemas.selection import SELECTABLE_ACTIONS, CandidateStatus, SelectedTarget, SelectionOutcome, SelectionResult
from app.schemas.verification import VerificationResult
from app.services import target_selection
from app.services.approval_gate import ApprovalGate
from app.services.context import ContextService

DEFAULT_MAX_REPLANS = 2
# LangGraph's default recursion limit (25 node steps) is lower than a legitimate
# multi-level DAG with its full replan budget needs (each dispatch round is
# three steps). Termination is still guaranteed by max_replans and by
# duplicate-failure suppression; this only bounds a runaway graph.
GRAPH_RECURSION_LIMIT = 200

_VERIFICATION_TO_TASK_STATUS = {
    VerificationStatus.VERIFIED: TaskStatus.COMPLETED,
    VerificationStatus.NEEDS_REVIEW: TaskStatus.BLOCKED,
    VerificationStatus.FAILED: TaskStatus.FAILED,
}

# Inverse of app.agents.academic.agent.AcademicAgent's own status_map -- used
# only to reconstruct an approximate VerificationResult on resume, since the
# original (with its named checks/issues) is not itself persisted; only the
# AgentResultStatus it produced is (see AgentRun in app/db/models/mission.py).
_AGENT_RESULT_TO_VERIFICATION = {
    AgentResultStatus.SUCCESS: VerificationStatus.VERIFIED,
    AgentResultStatus.PARTIAL: VerificationStatus.NEEDS_REVIEW,
    AgentResultStatus.FAILED: VerificationStatus.FAILED,
}


def _event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:12]}"


def _task_by_id(plan: MissionPlan, task_id: str) -> MissionTask:
    return next(task for task in plan.tasks if task.task_id == task_id)


DUPLICATE_FAILURE_MESSAGE = "Execution stopped because the same verified failure occurred again without new information."

# Phase 12B: statuses whose stored response belongs to the current plan. A
# PENDING/SKIPPED task has not run under it, so any response on record for
# its id is from an earlier plan (or never existed) and is never shown.
_SUMMARIZED_STATUSES = {TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.FAILED}
_CANDIDATE_LABEL = (
    "Candidate events (recommendations only -- none has been selected for you, registered, "
    "or sent for approval):"
)
_SELECTION_GUIDANCE = (
    "Select one of the candidate events in the Mission Workspace (or name it by its exact title in a new "
    "request); it is then re-checked against your timetable and exams and sent for approval."
)


class SelectionError(Exception):
    """A target selection the server refuses outright (Phase 13). ``status_code``
    follows HTTP semantics so the API can pass it through unchanged."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now(as_of: Optional[str]) -> datetime:
    if as_of:
        d = date.fromisoformat(str(as_of))
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _provider_unavailable_message(outage: Dict[str, object]) -> str:
    """User-facing reason for a Phase 12C provider-outage stop."""
    reason = {"rate_limit": "rate limit", "timeout": "timeout", "network": "network error", "server_error": "server error"}.get(
        str(outage.get("kind")), "outage"
    )
    status = f", HTTP {outage['status_code']}" if outage.get("status_code") else ""
    wait = f", retry after {outage['retry_after_seconds']}s" if outage.get("retry_after_seconds") is not None else ""
    pending = ", ".join(str(t) for t in outage.get("task_ids", []))
    return (
        f"Mission paused: the LLM provider {outage.get('provider')} ({outage.get('model')}) is unavailable "
        f"({reason}{status}{wait}) after {outage.get('retries', 0)} retries. Nothing was replanned; completed results "
        f"are kept and the unfinished task(s) ({pending}) will run when the mission is resumed."
    )


def unconfirmed_action_targets(
    plan: MissionPlan, goal: str, selections: Optional[Mapping[str, SelectedTarget]] = None
) -> Dict[str, TargetProvenance]:
    """Action tasks whose target the student never named, keyed by task id.

    Deterministic: the target must appear in the student's own goal, or be
    the resource of the student's persisted selection for that task
    (``selections``, keyed by step id, loaded from the Context Service). These
    tasks are never dispatched -- no proposal, no approval -- because an
    agent's recommendation is not the student's selection.
    """
    unconfirmed: Dict[str, TargetProvenance] = {}
    for task in plan.tasks:
        if task.agent != AgentName.ACTION_AGENT:
            continue
        tool_name = str(task.constraints.get("tool_name") or "").strip()
        selection = (selections or {}).get(task.task_id)
        provenance = resolve_target_provenance(tool_name, task.constraints, goal, selection)
        if not provenance.confirmed:
            unconfirmed[task.task_id] = provenance
    return unconfirmed


def unselected_declared_actions(
    plan: MissionPlan, goal: str, selections: Optional[Mapping[str, SelectedTarget]] = None
) -> List[str]:
    """Tools the planner declared as awaiting the student's selection
    (``selection_required_actions``) for which the plan holds no action task
    with a confirmed target -- a named target always wins over the declaration."""
    unconfirmed = unconfirmed_action_targets(plan, goal, selections)
    confirmed_tools = {
        str(task.constraints.get("tool_name") or "")
        for task in plan.tasks
        if task.agent == AgentName.ACTION_AGENT and task.task_id not in unconfirmed
    }
    return [tool for tool in plan.selection_required_actions if tool not in confirmed_tools]


def pending_selection_tools(
    plan: Optional[MissionPlan], goal: str, selections: Optional[Mapping[str, SelectedTarget]] = None
) -> List[str]:
    """The action tools still waiting for the student to choose a target."""
    if plan is None:
        return []
    tools = [p.tool_name for p in unconfirmed_action_targets(plan, goal, selections).values()]
    tools += unselected_declared_actions(plan, goal, selections)
    return list(dict.fromkeys(tools))


def user_selection_required(
    plan: Optional[MissionPlan], goal: str, selections: Optional[Mapping[str, SelectedTarget]] = None
) -> bool:
    """True when an action the student asked for cannot proceed until they
    select its target: an action task was refused by target provenance, or the
    planner declared the action without a target. Structured state only --
    never the final response's prose. A persisted selection (``selections``)
    satisfies it once the continuation plan carries the selected action."""
    return bool(pending_selection_tools(plan, goal, selections))


def selection_locked(session: Session, selection: SelectedTarget) -> bool:
    """True once the selected action is approved or executed: from then on the
    selection can never change (an approval for one target never covers another)."""
    approval = get_latest_approval_for_step(session, selection.step_id)
    if approval is None:
        return False
    if approval.status == ApprovalStatus.APPROVED:
        return True
    tool_call = get_tool_call_by_id(session, approval.tool_call_id) if approval.tool_call_id else None
    return tool_call is not None and tool_call.status == ToolExecutionStatus.SUCCESS


def _task_identity(task: MissionTask) -> tuple:
    return (task.agent, task.objective, tuple(task.dependencies), repr(sorted(task.constraints.items())))


def redefined_task_ids(old_plan: MissionPlan, new_plan: MissionPlan) -> List[str]:
    """Ids a replan reused for a *different* task, plus everything downstream of them.

    Task ids are positional (``<mission>-task-<n>``), so a replan can put a
    new agent/objective/target under an id that already has a result. That
    result describes the old task and must not be carried over -- nor may
    any task built on it.
    """
    old_by_id = {task.task_id: task for task in old_plan.tasks}
    changed = {
        task.task_id
        for task in new_plan.tasks
        if task.task_id in old_by_id and _task_identity(task) != _task_identity(old_by_id[task.task_id])
    }
    grew = True
    while grew:
        grew = False
        for task in new_plan.tasks:
            if task.task_id not in changed and any(dep in changed for dep in task.dependencies):
                changed.add(task.task_id)
                grew = True
    return [task.task_id for task in new_plan.tasks if task.task_id in changed]


def _rebuild_failure_fingerprints(context: ContextService, mission_id: str) -> List[str]:
    """Every failure fingerprint this mission has already recorded, from the
    audit trail -- so a resumed mission (a new process, replan_count reset)
    still recognises a failure it has already seen."""
    seen: List[str] = []
    for event in context.list_audit_events(mission_id):
        fingerprint = (event.event_metadata or {}).get("failure_fingerprint")
        if fingerprint and fingerprint not in seen:
            seen.append(fingerprint)
    return seen


class MissionOrchestrator:
    """Builds and runs the LangGraph mission-execution state machine."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        registry: AgentRegistry,
        llm_provider: LLMProvider,
        max_replans: int = DEFAULT_MAX_REPLANS,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._llm_provider = llm_provider
        self._max_replans = max_replans
        # Serializes selection requests in this process, so two clicks on the
        # same candidate can never both create a proposal.
        self._selection_lock = threading.Lock()
        self._schema_ready = False
        self._graph = self._build_graph()

    def _ensure_schema(self) -> None:
        """Bring an existing database up to the current tables/columns once,
        before this orchestrator's first mission operation (additive and
        idempotent -- see ``upgrade_schema``). Lazy, so building an
        orchestrator (e.g. at API import time) never touches a database."""
        if self._schema_ready:
            return
        bind = self._session_factory.kw.get("bind")
        if bind is not None:
            upgrade_schema(bind)
        self._schema_ready = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_mission(
        self,
        goal: str,
        *,
        user_id: str,
        user_role: UserRole,
        student_id: str | None = None,
        as_of: str | None = None,
    ) -> OrchestratorState:
        """Plan and execute a brand-new mission for ``goal``."""
        self._ensure_schema()
        mission_id = f"mission-{uuid.uuid4().hex[:12]}"
        initial: OrchestratorState = {
            "mission_id": mission_id,
            "user_id": user_id,
            "user_role": user_role,
            "original_goal": goal,
            "student_id": student_id or user_id,
            "as_of": as_of,
            "plan": None,
            "validation_errors": [],
            "planning_error": None,
            "task_status": {},
            "ready_task_ids": [],
            "agent_results": {},
            "verifications": {},
            "responses": {},
            "mission_status": MissionStatus.PENDING,
            "replan_count": 0,
            "max_replans": self._max_replans,
            "failure_fingerprints": [],
            "round_failures": {},
            "duplicate_failure_stop": False,
            "provider_unavailable": None,
            "errors": [],
            "final_result": None,
        }
        return self._graph.invoke(initial, config={"configurable": {"thread_id": mission_id}, "recursion_limit": GRAPH_RECURSION_LIMIT})

    def resume_mission(self, mission_id: str) -> OrchestratorState:
        """Resume a mission after an interrupted execution.

        Rebuilds state entirely from the Context Service (the durable
        source of truth) -- never from a prior LangGraph checkpoint, which
        lives only in this process's memory (app/graph/checkpoint.py). This
        is what lets resumption survive a full process restart: a brand-new
        ``MissionOrchestrator`` (a fresh in-memory checkpointer, no shared
        state with whatever process ran the mission before) can still
        resume correctly by reading the SQLite-backed Mission/MissionStep/
        AuditLog rows alone.
        """
        self._ensure_schema()
        session = self._session_factory()
        try:
            context = ContextService(session)
            mission = context.get_mission(mission_id)
            if mission is None:
                raise ValueError(f"unknown mission_id: {mission_id!r}")
            plan = context.get_latest_plan_snapshot(mission_id)
            if plan is None:
                raise ValueError(f"mission {mission_id!r} has no persisted plan to resume from")

            task_status = {step.step_id: step.status for step in mission.steps}
            agent_results, verifications, responses = self._rebuild_results(context, mission_id)
            failure_fingerprints = _rebuild_failure_fingerprints(context, mission_id)
            mission_user_id, mission_user_role, mission_goal, prior_final_result = (
                mission.user_id,
                mission.user_role,
                mission.original_goal,
                mission.final_result,
            )
        finally:
            session.close()

        state: OrchestratorState = {
            "mission_id": mission_id,
            "user_id": mission_user_id,
            "user_role": mission_user_role,
            "original_goal": mission_goal,
            "student_id": mission_user_id,
            "as_of": None,
            "plan": plan,
            "validation_errors": [],
            "planning_error": None,
            "task_status": task_status,
            "ready_task_ids": [],
            "agent_results": agent_results,
            "verifications": verifications,
            "responses": responses,
            "mission_status": MissionStatus.IN_PROGRESS,
            "replan_count": 0,
            "max_replans": self._max_replans,
            "failure_fingerprints": failure_fingerprints,
            "round_failures": {},
            "duplicate_failure_stop": False,
            "provider_unavailable": None,
            "errors": [],
            "final_result": prior_final_result,
        }
        return self._graph.invoke(state, config={"configurable": {"thread_id": mission_id}, "recursion_limit": GRAPH_RECURSION_LIMIT})

    def select_target(
        self,
        mission_id: str,
        *,
        tool_name: str,
        resource_type: str,
        resource_id: int,
        selected_by: str,
        as_of: Optional[str] = None,
    ) -> SelectionOutcome:
        """Phase 13: the student picked one of the mission's candidates.

        Deterministic -- no LLM call. Validates the request against the
        mission's persisted candidates and the resource's *current* state,
        records the selection (the only USER_SELECTION authorization),
        writes the next plan version of the same mission and resumes it, so
        the Action Agent proposes, pre-checks and requests approval exactly as
        for a named target. A candidate that is no longer valid is BLOCKED:
        nothing is proposed and no approval is created.
        """
        self._ensure_schema()
        with self._selection_lock:
            outcome, continue_run = self._apply_selection(
                mission_id, tool_name=tool_name, resource_type=resource_type, resource_id=resource_id,
                selected_by=selected_by, now=_now(as_of),
            )
            if continue_run:
                self.resume_mission(mission_id)
        return outcome

    def _apply_selection(
        self, mission_id: str, *, tool_name: str, resource_type: str, resource_id: int, selected_by: str, now: datetime
    ) -> Tuple[SelectionOutcome, bool]:
        session = self._session_factory()
        try:
            context = ContextService(session)
            mission = context.get_mission(mission_id)
            if mission is None:
                raise SelectionError(404, f"unknown mission_id {mission_id!r}")
            plan = context.get_latest_plan_snapshot(mission_id)
            if plan is None:
                raise SelectionError(409, "This mission has no plan to continue.")
            goal, student_id = mission.original_goal, mission.user_id
            selections = context.list_active_target_selections(mission_id)
            candidate_tools = {r.tool_name for r in context.list_action_candidates(mission_id)}
            if tool_name not in candidate_tools:
                raise SelectionError(422, f"This mission has no candidates to select for {tool_name!r}.")
            current = next((s for s in selections.values() if s.tool_name == tool_name), None)
            if current is None and tool_name not in pending_selection_tools(plan, goal, selections):
                raise SelectionError(409, "This mission does not need a target selection.")
            record = context.get_action_candidate(mission_id, tool_name, resource_type, resource_id)
            if record is None:
                raise SelectionError(404, f"{resource_type} {resource_id} is not one of this mission's candidates.")

            # Once the current selection's action is approved or executed, the
            # selection can no longer change (an approval for A never covers B).
            if current is not None and selection_locked(session, current):
                raise SelectionError(
                    409, f"The action for '{current.title}' is already approved or completed; the selection can no longer change."
                )

            was_selectable = record.status == CandidateStatus.ELIGIBLE.value
            assessment = target_selection.reassess_candidate(session, record, student_id=student_id, now=now)
            candidate = target_selection.to_candidate(record, selections, session)
            if not assessment.selectable:
                message = (
                    f"'{record.title}' can no longer be selected: {' '.join(assessment.reasons)} "
                    "This changed after it was recommended. Please choose another event."
                    if was_selectable
                    else f"'{record.title}' cannot be selected: {' '.join(assessment.reasons)} Please choose another event."
                )
                context.append_audit_event(
                    event_id=_event_id(), mission_id=mission_id, event_type="target_selection_blocked", actor=selected_by,
                    message=message,
                    metadata={"tool_name": tool_name, "resource_type": resource_type, "resource_id": resource_id,
                              "status": assessment.status.value, "reasons": assessment.reasons},
                )
                return SelectionOutcome(result=SelectionResult.BLOCKED, message=message, candidate=candidate), False

            if current is not None and current.resource_id == resource_id:
                approval = get_latest_approval_for_step(session, current.step_id)
                if approval is not None and approval.status == ApprovalStatus.PENDING:
                    return SelectionOutcome(
                        result=SelectionResult.ALREADY_SELECTED,
                        message=f"'{current.title}' is already selected and awaiting approval.",
                        candidate=candidate, selection=current,
                    ), False
                # Selected before but no live proposal (e.g. it went stale):
                # continue again under the same selection -- a fresh proposal
                # must pass the pre-check before any new approval.
                self._persist_continuation(context, plan, current)
                return SelectionOutcome(
                    result=SelectionResult.SELECTED, message=f"Re-checking '{current.title}' for a new approval.",
                    candidate=candidate, selection=current,
                ), True

            superseded_approval_id = None
            if current is not None:
                superseded_approval_id = self._supersede_selection(session, current, new_title=record.title, actor=selected_by)

            step_id = continuation_step_id(
                plan, tool_name=tool_name, selections=selections,
                unconfirmed_task_ids=unconfirmed_action_targets(plan, goal, selections).keys(),
                existing_step_ids=[step.step_id for step in mission.steps],
            )
            saved = context.record_target_selection(
                selection_id=f"sel-{uuid.uuid4().hex[:12]}", mission_id=mission_id, step_id=step_id,
                tool_name=tool_name, resource_type=resource_type, resource_id=resource_id, title=record.title,
                selected_by=selected_by,
            )
            selection = context.get_active_target_selection(mission_id, step_id)
            assert selection is not None and selection.selection_id == saved.selection_id
            context.append_audit_event(
                event_id=_event_id(), mission_id=mission_id, step_id=step_id, event_type="target_selected",
                actor=selected_by,
                message=f"Student selected '{record.title}' for {tool_name} (target source: user_selection).",
                metadata={"selection": selection.model_dump(mode="json"), "assessment": assessment.model_dump(mode="json"),
                          "superseded_selection_id": current.selection_id if current else None},
            )
            self._persist_continuation(context, plan, selection)
            return SelectionOutcome(
                result=SelectionResult.SELECTED, message=f"Selected: {record.title}",
                candidate=target_selection.to_candidate(record, {step_id: selection}, session), selection=selection,
                superseded_selection_id=current.selection_id if current else None,
                superseded_approval_id=superseded_approval_id,
            ), True
        finally:
            session.close()

    @staticmethod
    def _supersede_selection(session: Session, current: SelectedTarget, *, new_title: str, actor: str) -> Optional[str]:
        """The student chose a different target before approval. A pending
        approval for the old target is marked EDIT_REQUIRED (the existing
        lifecycle for a changed payload) and its tool call can never run."""
        context = ContextService(session)
        reason = f"The student selected '{new_title}' instead of '{current.title}'."
        superseded_approval_id = None
        approval = get_latest_approval_for_step(session, current.step_id)
        if approval is not None and approval.status == ApprovalStatus.PENDING:
            ApprovalGate(session).mark_superseded(approval.approval_id, requested_by=actor, reason=reason)
            tool_call = get_tool_call_by_id(session, approval.tool_call_id) if approval.tool_call_id else None
            if tool_call is not None and tool_call.status == ToolExecutionStatus.PENDING:
                context.update_tool_call_record(
                    tool_call.tool_call_id, status=ToolExecutionStatus.FAILED,
                    error="never executed; its approval was superseded by a new selection: " + reason,
                )
            superseded_approval_id = approval.approval_id
        context.supersede_target_selection(current.selection_id, reason=reason)
        context.append_audit_event(
            event_id=_event_id(), mission_id=current.mission_id, step_id=current.step_id,
            event_type="target_selection_superseded", actor=actor, message=reason,
            metadata={"selection_id": current.selection_id, "superseded_approval_id": superseded_approval_id},
        )
        return superseded_approval_id

    @staticmethod
    def _persist_continuation(context: ContextService, plan: MissionPlan, selection: SelectedTarget) -> None:
        """Write the next plan version (same mission) carrying the selected
        action, and make its step runnable. ``resume_mission`` then continues
        from the Context Service alone -- no planner call."""
        mission_id = plan.mission_id
        schedule = target_selection.verified_schedule(context, mission_id, plan)
        record = context.get_action_candidate(mission_id, selection.tool_name, selection.resource_type, selection.resource_id)
        upstream = [*(schedule.step_ids if schedule else []), *([record.recommended_by_step_id] if record and record.recommended_by_step_id else [])]
        new_plan = build_selection_continuation_plan(plan, step_id=selection.step_id, selection=selection, upstream_step_ids=upstream)
        mission = context.get_mission(mission_id)
        existing = {step.step_id for step in mission.steps} if mission else set()
        action = _task_by_id(new_plan, selection.step_id)
        if selection.step_id in existing:
            context.update_mission_step(selection.step_id, status=TaskStatus.PENDING, agent=action.agent, objective=action.objective)
        else:
            context.create_mission_step(
                step_id=selection.step_id, mission_id=mission_id, agent=action.agent, objective=action.objective,
                sequence=len(new_plan.tasks) - 1,
            )
        context.append_audit_event(
            event_id=_event_id(), mission_id=mission_id, event_type="plan_generated", actor="mission_orchestrator",
            message=f"Plan continued with the student's selection ('{selection.title}'); no replanning.",
            metadata={"plan": new_plan.model_dump(mode="json"), "source": "user_selection", "selection_id": selection.selection_id},
        )

    def _rebuild_results(
        self, context: ContextService, mission_id: str
    ) -> Tuple[Dict[str, AgentResult], Dict[str, VerificationResult], Dict[str, str]]:
        agent_results: Dict[str, AgentResult] = {}
        verifications: Dict[str, VerificationResult] = {}
        responses: Dict[str, str] = {}
        for run in context.list_agent_runs(mission_id):
            facts = dict(run.facts or {})
            response_text = facts.pop("_response_text", "")
            agent_results[run.step_id] = AgentResult(
                mission_id=mission_id,
                task_id=run.step_id,
                agent=run.agent,
                status=run.status,
                facts=facts,
                errors=list(run.errors or []),
                evidence=[Evidence.model_validate(e) for e in (run.evidence or [])],
                started_at=run.started_at,
                completed_at=run.completed_at,
            )
            responses[run.step_id] = response_text
            verifications[run.step_id] = VerificationResult(
                verification_id=f"reconstructed-{run.step_id}",
                mission_id=mission_id,
                task_id=run.step_id,
                phase=VerificationPhase.POST_ACTION,
                status=_AGENT_RESULT_TO_VERIFICATION.get(run.status, VerificationStatus.FAILED),
            )
        return agent_results, verifications, responses

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self):
        graph = StateGraph(OrchestratorState)
        graph.add_node("load_context", self._load_context)
        graph.add_node("generate_plan", self._generate_plan)
        graph.add_node("validate_plan", self._validate_plan)
        graph.add_node("schedule_ready_tasks", self._schedule_ready_tasks)
        graph.add_node("dispatch_and_collect", self._dispatch_and_collect)
        graph.add_node("update_mission_state", self._update_mission_state)
        graph.add_node("replan", self._replan)
        graph.add_node("finalize", self._finalize)

        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "generate_plan")
        graph.add_conditional_edges(
            "generate_plan",
            self._route_after_planning,
            {"finalize": "finalize", "validate_plan": "validate_plan"},
        )
        graph.add_conditional_edges(
            "validate_plan",
            self._route_after_validate,
            {"finalize": "finalize", "schedule_ready_tasks": "schedule_ready_tasks"},
        )
        graph.add_conditional_edges(
            "schedule_ready_tasks",
            self._route_after_schedule,
            {"dispatch_and_collect": "dispatch_and_collect", "update_mission_state": "update_mission_state"},
        )
        graph.add_edge("dispatch_and_collect", "update_mission_state")
        graph.add_conditional_edges(
            "update_mission_state",
            self._route_after_update,
            {"finalize": "finalize", "replan": "replan", "schedule_ready_tasks": "schedule_ready_tasks"},
        )
        graph.add_conditional_edges(
            "replan",
            self._route_after_planning,
            {"finalize": "finalize", "validate_plan": "validate_plan"},
        )
        graph.add_edge("finalize", END)

        return graph.compile(checkpointer=build_checkpointer())

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def _load_context(self, state: OrchestratorState) -> dict:
        session = self._session_factory()
        try:
            context = ContextService(session)
            mission_id = state["mission_id"]
            existing = context.get_mission(mission_id)
            if existing is None:
                context.create_mission(mission_id, state["user_id"], state["user_role"], state["original_goal"])
                context.append_audit_event(
                    event_id=_event_id(),
                    mission_id=mission_id,
                    event_type="mission_created",
                    actor="mission_orchestrator",
                    message=f"Mission created for goal: {state['original_goal']}",
                )
                mission_status = MissionStatus.PLANNING
            else:
                context.append_audit_event(
                    event_id=_event_id(),
                    mission_id=mission_id,
                    event_type="mission_resumed",
                    actor="mission_orchestrator",
                    message="Mission execution resumed after interruption.",
                )
                mission_status = existing.status
            context.update_mission_status(mission_id, mission_status)
        finally:
            session.close()
        return {"mission_status": mission_status}

    def _generate_plan(self, state: OrchestratorState) -> dict:
        if state.get("plan") is not None:
            return {}  # resuming, or a replan already produced one this round
        try:
            plan = generate_plan(
                self._llm_provider,
                state["mission_id"],
                state["original_goal"],
                supported_agents=self._registry.supported_agents(),
            )
        except Exception as exc:  # noqa: BLE001 -- a planner failure fails the mission explicitly, never crashes it mid-PLANNING
            return self._record_planning_failure(state, exc)
        return {"plan": plan}

    def _record_planning_failure(self, state: OrchestratorState, exc: Exception) -> dict:
        """Persist a planner failure as a FAILED mission with an audit event.

        Without this, an exception from the planner (e.g. the live LLM being
        unreachable) would escape ``graph.invoke`` and leave the persisted
        mission stuck in PLANNING with no explanation. No fallback plan is
        ever substituted -- the mission simply fails, visibly.
        """
        message = f"{type(exc).__name__}: {exc}"
        session = self._session_factory()
        try:
            context = ContextService(session)
            context.append_audit_event(
                event_id=_event_id(),
                mission_id=state["mission_id"],
                event_type="plan_generation_failed",
                actor="mission_orchestrator",
                message=f"Planner failed: {message}",
                metadata=(
                    exc.details()
                    if isinstance(exc, LLMTransientError)
                    else {"provider": getattr(self._llm_provider, "name", "unknown")}
                ),
            )
            context.update_mission_status(state["mission_id"], MissionStatus.FAILED)
        finally:
            session.close()
        return {"planning_error": message, "mission_status": MissionStatus.FAILED}

    def _validate_plan(self, state: OrchestratorState) -> dict:
        plan = state["plan"]
        assert plan is not None
        result = validate_plan(plan, self._registry, expected_mission_id=state["mission_id"])

        session = self._session_factory()
        try:
            context = ContextService(session)
            if not result.is_valid:
                context.append_audit_event(
                    event_id=_event_id(),
                    mission_id=state["mission_id"],
                    event_type="plan_invalid",
                    actor="mission_orchestrator",
                    message="Generated plan failed validation.",
                    metadata={"errors": result.errors, "unsupported_requests": list(plan.unsupported_requests)},
                )
                context.update_mission_status(state["mission_id"], MissionStatus.FAILED)
                return {"validation_errors": result.errors, "mission_status": MissionStatus.FAILED}

            mission = context.get_mission(state["mission_id"])
            existing_step_ids = {step.step_id for step in mission.steps} if mission else set()
            for sequence, task in enumerate(plan.tasks):
                if task.task_id not in existing_step_ids:
                    context.create_mission_step(
                        step_id=task.task_id,
                        mission_id=state["mission_id"],
                        agent=task.agent,
                        objective=task.objective,
                        sequence=sequence,
                    )
            context.append_audit_event(
                event_id=_event_id(),
                mission_id=state["mission_id"],
                event_type="plan_generated",
                actor="mission_orchestrator",
                message=f"Validated plan with {len(plan.tasks)} task(s).",
                metadata={"plan": plan.model_dump(mode="json")},
            )
            task_status = self._skip_unconfirmed_action_targets(context, state, plan)
            context.update_mission_status(state["mission_id"], MissionStatus.IN_PROGRESS)
        finally:
            session.close()
        return {"validation_errors": [], "mission_status": MissionStatus.IN_PROGRESS, "task_status": task_status}

    @staticmethod
    def _skip_unconfirmed_action_targets(context: ContextService, state: OrchestratorState, plan: MissionPlan) -> Dict[str, TaskStatus]:
        """Mark every action task whose target the student never named SKIPPED
        before anything is dispatched, so it can never produce a proposal or an
        approval. Its read-only upstream tasks still run, so candidates are
        still shown -- as candidates, for the student to choose from."""
        task_status = dict(state.get("task_status", {}))
        selections = context.list_active_target_selections(state["mission_id"])
        for task_id, provenance in unconfirmed_action_targets(plan, state["original_goal"], selections).items():
            if task_status.get(task_id) in (TaskStatus.COMPLETED, TaskStatus.SKIPPED):
                continue
            task_status[task_id] = TaskStatus.SKIPPED
            context.update_mission_step(task_id, status=TaskStatus.SKIPPED, completed_at=utc_now())
            context.append_audit_event(
                event_id=_event_id(),
                mission_id=state["mission_id"],
                step_id=task_id,
                event_type="action_target_unconfirmed",
                actor="mission_orchestrator",
                message=(
                    f"Action {provenance.tool_name} was not prepared: {provenance.reason} "
                    "No proposal or approval was created; the student must select the target explicitly."
                ),
                metadata={"target_provenance": provenance.model_dump(mode="json"), "user_selection_required": True},
            )
        recorded = {
            (e.event_metadata or {}).get("tool_name")
            for e in context.list_audit_events(state["mission_id"])
            if e.event_type == "action_target_unconfirmed" and (e.event_metadata or {}).get("source") == "planner"
        }
        for tool_name in unselected_declared_actions(plan, state["original_goal"], selections):
            if tool_name in recorded:
                continue
            context.append_audit_event(
                event_id=_event_id(),
                mission_id=state["mission_id"],
                event_type="action_target_unconfirmed",
                actor="mission_orchestrator",
                message=(
                    f"Action {tool_name} was not planned: the student has not selected its target. "
                    "No proposal or approval was created; the student must select the target explicitly."
                ),
                metadata={"tool_name": tool_name, "source": "planner", "user_selection_required": True},
            )
        return task_status

    def _schedule_ready_tasks(self, state: OrchestratorState) -> dict:
        plan = state["plan"]
        assert plan is not None
        ready, updated_status = compute_ready_and_blocked(plan, state.get("task_status", {}))
        return {"task_status": updated_status, "ready_task_ids": ready}

    def _dispatch_and_collect(self, state: OrchestratorState) -> dict:
        plan = state["plan"]
        assert plan is not None
        ready = state.get("ready_task_ids", [])

        base_facts: dict = {"student_id": state["student_id"], MISSION_GOAL_KEY: state["original_goal"]}
        if state.get("as_of"):
            base_facts["as_of"] = state["as_of"]

        outcomes = dispatch_ready_tasks(
            plan,
            ready,
            base_facts=base_facts,
            agent_results=state.get("agent_results", {}),
            registry=self._registry,
            session_factory=self._session_factory,
        )

        prior_agent_results = state.get("agent_results", {})
        task_status = dict(state.get("task_status", {}))
        agent_results = dict(prior_agent_results)
        verifications = dict(state.get("verifications", {}))
        responses = dict(state.get("responses", {}))
        errors = list(state.get("errors", []))
        known_fingerprints = list(state.get("failure_fingerprints", []))
        round_failures: Dict[str, Dict[str, object]] = {}
        provider_unavailable: Optional[Dict[str, object]] = None

        def _fingerprint_failure(task_id: str, verification_status: str, reasons: List[str]) -> dict:
            task = _task_by_id(plan, task_id)
            fingerprint = failure_fingerprint(
                task,
                mission_id=state["mission_id"],
                verification_status=verification_status,
                reasons=reasons,
                input_facts=build_task_input_facts(task, base_facts, prior_agent_results),
            )
            repeated = fingerprint in known_fingerprints
            if not repeated:
                known_fingerprints.append(fingerprint)
            round_failures[task_id] = {"fingerprint": fingerprint, "repeated": repeated}
            return {"failure_fingerprint": fingerprint, "repeat_of_earlier_failure": repeated}

        session = self._session_factory()
        try:
            context = ContextService(session)
            for task_id, dispatched in outcomes.items():
                context.update_mission_step(task_id, status=TaskStatus.IN_PROGRESS, started_at=utc_now())

                if dispatched.provider_unavailable is not None:
                    # Phase 12C: an infrastructure outage, not a task failure.
                    # The task stays PENDING (a resume runs it again), no
                    # failure fingerprint is taken and no replan is triggered.
                    task_status[task_id] = TaskStatus.PENDING
                    errors.append(f"{task_id}: {dispatched.error}")
                    provider_unavailable = {**dispatched.provider_unavailable, "task_ids": [
                        *(provider_unavailable or {}).get("task_ids", []), task_id,
                    ]}
                    context.update_mission_step(task_id, status=TaskStatus.PENDING)
                    context.append_audit_event(
                        event_id=_event_id(),
                        mission_id=state["mission_id"],
                        step_id=task_id,
                        event_type="provider_unavailable",
                        actor="mission_orchestrator",
                        message=f"Task {task_id} was not run: the LLM provider is unavailable ({dispatched.error}).",
                        metadata=dict(dispatched.provider_unavailable),
                    )
                    continue

                if dispatched.outcome is None:
                    task_status[task_id] = TaskStatus.FAILED
                    error_message = dispatched.error or "dispatch failed"
                    errors.append(f"{task_id}: {error_message}")
                    failure_metadata = _fingerprint_failure(task_id, "dispatch_error", [error_message])
                    context.record_agent_result(
                        mission_id=state["mission_id"],
                        step_id=task_id,
                        agent=_task_by_id(plan, task_id).agent,
                        status=AgentResultStatus.FAILED,
                        facts={},
                        errors=[error_message],
                    )
                    context.update_mission_step(task_id, status=TaskStatus.FAILED, completed_at=utc_now())
                    context.append_audit_event(
                        event_id=_event_id(),
                        mission_id=state["mission_id"],
                        step_id=task_id,
                        event_type="task_failed",
                        actor="mission_orchestrator",
                        message=f"Task {task_id} failed to execute: {error_message}",
                        metadata=failure_metadata,
                    )
                    continue

                outcome = dispatched.outcome
                agent_results[task_id] = outcome.agent_result
                verifications[task_id] = outcome.verification
                responses[task_id] = outcome.response_text
                new_status = _VERIFICATION_TO_TASK_STATUS[outcome.verification.status]
                task_status[task_id] = new_status

                facts_to_persist = dict(outcome.agent_result.facts)
                facts_to_persist["_response_text"] = outcome.response_text
                context.record_agent_result(
                    mission_id=state["mission_id"],
                    step_id=task_id,
                    agent=outcome.agent_result.agent,
                    status=outcome.agent_result.status,
                    facts=facts_to_persist,
                    errors=outcome.agent_result.errors,
                    evidence=[e.model_dump(mode="json") for e in outcome.agent_result.evidence],
                    started_at=outcome.agent_result.started_at,
                    completed_at=outcome.agent_result.completed_at,
                )
                context.update_mission_step(task_id, status=new_status, completed_at=utc_now())
                verified_metadata: dict = {"issues": outcome.verification.issues}
                if new_status == TaskStatus.FAILED:
                    verified_metadata.update(
                        _fingerprint_failure(
                            task_id,
                            outcome.verification.status.value,
                            list(outcome.verification.issues) + list(outcome.agent_result.errors),
                        )
                    )
                context.append_audit_event(
                    event_id=_event_id(),
                    mission_id=state["mission_id"],
                    step_id=task_id,
                    event_type="task_verified",
                    actor="mission_orchestrator",
                    message=f"Task {task_id} verification: {outcome.verification.status.value}",
                    metadata=verified_metadata,
                )
        finally:
            session.close()

        return {
            "task_status": task_status,
            "agent_results": agent_results,
            "verifications": verifications,
            "responses": responses,
            "errors": errors,
            "ready_task_ids": [],
            "failure_fingerprints": known_fingerprints,
            "round_failures": round_failures,
            "provider_unavailable": provider_unavailable,
        }

    def _update_mission_state(self, state: OrchestratorState) -> dict:
        plan = state["plan"]
        assert plan is not None

        # Recompute the cascade now -- not just on the next
        # schedule_ready_tasks call -- so a task that just failed this round
        # immediately propagates SKIPPED to its dependents in the state
        # we're about to finalize on, rather than leaving them dangling as
        # PENDING forever if this turns out to be the mission's last round.
        ready, cascaded_status = compute_ready_and_blocked(plan, state.get("task_status", {}))
        newly_skipped = [
            task_id
            for task_id, status in cascaded_status.items()
            if status == TaskStatus.SKIPPED and state.get("task_status", {}).get(task_id) != TaskStatus.SKIPPED
        ]
        if newly_skipped:
            self._persist_skipped_tasks(state["mission_id"], newly_skipped)
        task_status = cascaded_status

        if state.get("provider_unavailable"):
            # Phase 12C: an LLM outage is not evidence the plan is wrong, so it
            # never reaches the replan branch below and never spends the replan
            # budget. Stop with every result kept; a resume re-runs the
            # PENDING task(s) under the same plan.
            self._persist_mission_status(state["mission_id"], MissionStatus.FAILED)
            return {"mission_status": MissionStatus.FAILED, "task_status": task_status}

        if has_blocked_tasks(task_status):
            mission_status = MissionStatus.NEEDS_APPROVAL
        elif has_failed_tasks(task_status):
            failed_ids = [task_id for task_id, status in task_status.items() if status == TaskStatus.FAILED]
            round_failures = state.get("round_failures", {})
            repeated_ids = [task_id for task_id, info in round_failures.items() if info.get("repeated")]
            if all_failures_repeated(failed_ids, repeated_ids):
                # Nothing about these failures changed since the last attempt
                # (same task, target, reasons and input facts) -- another
                # replan cannot produce new information. Stop, visibly.
                self._record_duplicate_failure(state, failed_ids, round_failures)
                self._persist_mission_status(state["mission_id"], MissionStatus.FAILED)
                errors = list(state.get("errors", []))
                errors.append(DUPLICATE_FAILURE_MESSAGE)
                return {
                    "mission_status": MissionStatus.FAILED,
                    "task_status": task_status,
                    "errors": errors,
                    "duplicate_failure_stop": True,
                }
            if state["replan_count"] < state["max_replans"]:
                mission_status = MissionStatus.NEEDS_REPLAN
            else:
                mission_status = MissionStatus.FAILED
        elif is_mission_complete(plan, task_status):
            mission_status = MissionStatus.COMPLETED
        elif not ready:
            # Should be unreachable for a validated (acyclic, fully-
            # referenced) plan: a defensive stop rather than an unbounded
            # loop if this is ever somehow reached.
            mission_status = MissionStatus.FAILED
            errors = list(state.get("errors", []))
            errors.append("Scheduling deadlock: no ready, blocked, or failed tasks, but the mission is incomplete.")
            self._persist_mission_status(state["mission_id"], mission_status)
            return {"mission_status": mission_status, "task_status": task_status, "errors": errors}
        else:
            mission_status = MissionStatus.IN_PROGRESS

        self._persist_mission_status(state["mission_id"], mission_status)
        return {"mission_status": mission_status, "task_status": task_status}

    def _persist_skipped_tasks(self, mission_id: str, task_ids: List[str]) -> None:
        session = self._session_factory()
        try:
            context = ContextService(session)
            for task_id in task_ids:
                context.update_mission_step(task_id, status=TaskStatus.SKIPPED, completed_at=utc_now())
                context.append_audit_event(
                    event_id=_event_id(),
                    mission_id=mission_id,
                    step_id=task_id,
                    event_type="task_skipped",
                    actor="mission_orchestrator",
                    message=f"Task {task_id} skipped: a prerequisite failed or was skipped.",
                )
        finally:
            session.close()

    def _record_duplicate_failure(
        self, state: OrchestratorState, failed_ids: List[str], round_failures: Dict[str, Dict[str, object]]
    ) -> None:
        session = self._session_factory()
        try:
            ContextService(session).append_audit_event(
                event_id=_event_id(),
                mission_id=state["mission_id"],
                event_type="duplicate_failure_detected",
                actor="mission_orchestrator",
                message=(
                    f"{DUPLICATE_FAILURE_MESSAGE} Repeated failure(s): {', '.join(sorted(failed_ids))} "
                    f"(after {state['replan_count']} replan(s); limit {state['max_replans']})."
                ),
                metadata={
                    "task_ids": sorted(failed_ids),
                    "fingerprints": {task_id: round_failures[task_id]["fingerprint"] for task_id in sorted(failed_ids)},
                    "replan_count": state["replan_count"],
                    "max_replans": state["max_replans"],
                },
            )
        finally:
            session.close()

    def _persist_mission_status(self, mission_id: str, status: MissionStatus) -> None:
        session = self._session_factory()
        try:
            context = ContextService(session)
            context.update_mission_status(mission_id, status)
        finally:
            session.close()

    def _replan(self, state: OrchestratorState) -> dict:
        session = self._session_factory()
        try:
            context = ContextService(session)
            context.append_audit_event(
                event_id=_event_id(),
                mission_id=state["mission_id"],
                event_type="replan_triggered",
                actor="mission_orchestrator",
                message=f"Replanning after task failure (attempt {state['replan_count'] + 1}/{state['max_replans']}).",
                metadata={
                    "failed_tasks": {
                        task_id: info["fingerprint"] for task_id, info in state.get("round_failures", {}).items()
                    }
                },
            )
            context.update_mission_status(state["mission_id"], MissionStatus.NEEDS_REPLAN)
        finally:
            session.close()

        try:
            new_plan = generate_plan(
                self._llm_provider,
                state["mission_id"],
                state["original_goal"],
                supported_agents=self._registry.supported_agents(),
            )
        except Exception as exc:  # noqa: BLE001 -- see _record_planning_failure
            return self._record_planning_failure(state, exc)
        task_status = dict(state.get("task_status", {}))
        for task_id, status in list(task_status.items()):
            if status == TaskStatus.FAILED:
                task_status[task_id] = TaskStatus.PENDING

        agent_results = dict(state.get("agent_results", {}))
        verifications = dict(state.get("verifications", {}))
        responses = dict(state.get("responses", {}))
        old_plan = state["plan"]
        redefined = redefined_task_ids(old_plan, new_plan) if old_plan is not None else []
        new_ids = {task.task_id for task in new_plan.tasks}
        superseded = [
            task.task_id
            for task in (old_plan.tasks if old_plan is not None else [])
            if task.task_id not in new_ids and task_status.get(task.task_id) not in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)
        ]
        for task_id in redefined:
            task_status[task_id] = TaskStatus.PENDING
            for results in (agent_results, verifications, responses):
                results.pop(task_id, None)
        for task_id in superseded:
            task_status[task_id] = TaskStatus.SKIPPED
        self._persist_replan_task_changes(state["mission_id"], new_plan, redefined, superseded)

        return {
            "plan": new_plan,
            "task_status": task_status,
            "agent_results": agent_results,
            "verifications": verifications,
            "responses": responses,
            "replan_count": state["replan_count"] + 1,
            "round_failures": {},
        }

    def _persist_replan_task_changes(
        self, mission_id: str, new_plan: MissionPlan, redefined: List[str], superseded: List[str]
    ) -> None:
        if not redefined and not superseded:
            return
        tasks_by_id = {task.task_id: task for task in new_plan.tasks}
        session = self._session_factory()
        try:
            context = ContextService(session)
            existing = {step.step_id for step in context.get_mission(mission_id).steps}
            for task_id in redefined:
                if task_id not in existing:
                    continue  # created fresh by validate_plan
                task = tasks_by_id[task_id]
                context.update_mission_step(task_id, status=TaskStatus.PENDING, agent=task.agent, objective=task.objective)
                context.append_audit_event(
                    event_id=_event_id(),
                    mission_id=mission_id,
                    step_id=task_id,
                    event_type="task_redefined",
                    actor="mission_orchestrator",
                    message=f"Replan assigned {task_id} to a different task; its earlier result is not reused.",
                    metadata={"agent": task.agent.value, "objective": task.objective},
                )
            for task_id in superseded:
                context.update_mission_step(task_id, status=TaskStatus.SKIPPED, completed_at=utc_now())
                context.append_audit_event(
                    event_id=_event_id(),
                    mission_id=mission_id,
                    step_id=task_id,
                    event_type="task_superseded",
                    actor="mission_orchestrator",
                    message=f"Task {task_id} is not part of the new plan and will not run.",
                )
        finally:
            session.close()

    def _finalize(self, state: OrchestratorState) -> dict:
        session = self._session_factory()
        try:
            context = ContextService(session)
            selections = context.list_active_target_selections(state["mission_id"])
            candidate_note = self._record_candidates(session, state, selections)
            final_text = self._summarize(state, selections, candidate_note)
            context.update_mission_status(state["mission_id"], state["mission_status"], final_result=final_text)
            context.append_audit_event(
                event_id=_event_id(),
                mission_id=state["mission_id"],
                event_type="mission_finalized",
                actor="mission_orchestrator",
                message=f"Mission finalized with status {state['mission_status'].value}.",
            )
        finally:
            session.close()
        return {"final_result": final_text}

    @staticmethod
    def _record_candidates(session: Session, state: OrchestratorState, selections: Mapping[str, SelectedTarget]) -> str:
        """Phase 13: when a completed mission is waiting for the student to pick
        an action's target, persist the verified candidates (deterministic, no
        LLM) and return one line saying how many can be selected."""
        plan = state.get("plan")
        if plan is None or state["mission_status"] != MissionStatus.COMPLETED:
            return ""
        tools = [t for t in pending_selection_tools(plan, state["original_goal"], selections) if t in SELECTABLE_ACTIONS]
        if not tools:
            return ""
        candidates = target_selection.record_candidates(
            session, mission_id=state["mission_id"], plan=plan, tools=tools,
            student_id=state["student_id"], now=_now(state.get("as_of")),
        )
        counts: Dict[str, int] = {}
        for candidate in candidates:
            counts[candidate.assessment.status.value] = counts.get(candidate.assessment.status.value, 0) + 1
        ContextService(session).append_audit_event(
            event_id=_event_id(),
            mission_id=state["mission_id"],
            event_type="action_candidates_recorded",
            actor="mission_orchestrator",
            message=f"Recorded {len(candidates)} candidate target(s) for {', '.join(tools)}; the student selects one.",
            metadata={"tools": tools, "status_counts": counts},
        )
        if not candidates:
            return "No candidate events were found to select from."
        eligible = counts.get(CandidateStatus.ELIGIBLE.value, 0)
        return (
            f"{eligible} of {len(candidates)} candidate event(s) can be selected now; the others show why they "
            "cannot. Nothing is registered until you select one and an approver approves it."
        )

    @staticmethod
    def _summarize(
        state: OrchestratorState,
        selections: Optional[Mapping[str, SelectedTarget]] = None,
        candidate_note: str = "",
    ) -> str:
        plan = state.get("plan")
        responses = state.get("responses", {})
        task_status = state.get("task_status", {})
        unsupported = list(plan.unsupported_requests) if plan is not None else []
        selections = selections or {}
        # Phase 12B: an action whose target the student never named was not
        # prepared. Say so, and present any events found as candidates -- an
        # agent's recommendation is never the student's selection.
        unconfirmed = unconfirmed_action_targets(plan, state["original_goal"], selections) if plan is not None else {}
        confirmed_registration = plan is not None and any(
            task.agent == AgentName.ACTION_AGENT
            and task.constraints.get("tool_name") == "register_event"
            and task.task_id not in unconfirmed
            for task in plan.tasks
        )
        label_candidates = bool(unconfirmed or unsupported) and not confirmed_registration
        # Iterate the plan's declared task order (not a lexical sort of task
        # ids) so a synthesized multi-agent result reads in the mission's
        # actual sequence -- matters once task ids exceed one digit, and is
        # the more correct ordering either way.
        tasks = list(plan.tasks) if plan is not None else []
        ordered_task_ids = [task.task_id for task in tasks] if plan is not None else sorted(responses)
        agents = {task.task_id: task.agent for task in tasks}
        # Skip a task's answer when an earlier task's answer already contains
        # it verbatim (e.g. an eligibility answer that already states the
        # recovery path), so the synthesis doesn't repeat itself.
        parts: List[str] = []
        for task_id in ordered_task_ids:
            if plan is not None and task_status.get(task_id) not in _SUMMARIZED_STATUSES:
                continue
            answer = (responses.get(task_id) or "").strip()
            if answer and not any(answer in earlier for earlier in parts):
                if label_candidates and agents.get(task_id) == AgentName.EVENTS_OPPORTUNITY_AGENT:
                    answer = f"{_CANDIDATE_LABEL}\n{answer}"
                parts.append(answer)
        gathered = " ".join(parts)
        selection_notes = [
            f"User selection required: {provenance.tool_name} was not prepared -- {provenance.reason} {_SELECTION_GUIDANCE}"
            for provenance in unconfirmed.values()
        ]

        if state.get("planning_error"):
            text = f"Mission failed: the planner could not produce a usable plan ({state['planning_error']})."
            return f"{text} Results gathered before the failure: {gathered}" if gathered else f"{text} No task was executed."
        if state.get("validation_errors"):
            if plan is not None and not plan.tasks and unsupported:
                return "CampusNexus can't help with this goal: " + " ".join(unsupported)
            return "Mission failed: plan validation errors -- " + "; ".join(state["validation_errors"])

        text = gathered or f"Mission ended with status {state['mission_status'].value}."
        outage = state.get("provider_unavailable")
        if outage:
            text = f"{_provider_unavailable_message(outage)} {gathered}".strip()
        if state.get("duplicate_failure_stop"):
            text = f"{DUPLICATE_FAILURE_MESSAGE} {gathered}".strip()
        if selection_notes:
            text += " " + " ".join(selection_notes)
        if candidate_note:
            text += " " + candidate_note
        chosen = [s for s in selections.values() if plan is not None and s.step_id in {t.task_id for t in plan.tasks}]
        for selection in chosen:
            text += f" Selected by you: '{selection.title}' ({selection.tool_name})."
        if unsupported:
            # Phase 13: after a selection, the planner's original notes describe
            # the goal as it stood before the student chose -- label them so.
            prefix = "Planner notes from before your selection:" if chosen else "Not handled by this mission:"
            text += f" {prefix} " + " ".join(unsupported)
        return text

    # ------------------------------------------------------------------
    # Conditional routing
    # ------------------------------------------------------------------

    @staticmethod
    def _route_after_planning(state: OrchestratorState) -> str:
        return "finalize" if state.get("planning_error") else "validate_plan"

    @staticmethod
    def _route_after_validate(state: OrchestratorState) -> str:
        return "finalize" if state.get("validation_errors") else "schedule_ready_tasks"

    @staticmethod
    def _route_after_schedule(state: OrchestratorState) -> str:
        return "dispatch_and_collect" if state.get("ready_task_ids") else "update_mission_state"

    @staticmethod
    def _route_after_update(state: OrchestratorState) -> str:
        status = state["mission_status"]
        if status == MissionStatus.NEEDS_REPLAN:
            return "replan"
        if status in (MissionStatus.NEEDS_APPROVAL, MissionStatus.COMPLETED, MissionStatus.FAILED):
            return "finalize"
        return "schedule_ready_tasks"
