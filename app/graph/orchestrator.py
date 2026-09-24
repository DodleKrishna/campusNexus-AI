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

import uuid
from typing import Dict, List, Tuple

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import utc_now
from app.graph.checkpoint import build_checkpointer
from app.graph.dispatcher import build_task_input_facts, dispatch_ready_tasks
from app.graph.failures import all_failures_repeated, failure_fingerprint
from app.graph.planner import generate_plan
from app.graph.registry import AgentRegistry
from app.graph.scheduler import compute_ready_and_blocked, has_blocked_tasks, has_failed_tasks, is_mission_complete
from app.graph.state import OrchestratorState
from app.graph.validator import validate_plan
from app.llm.base import LLMProvider
from app.schemas.agent import AgentResult
from app.schemas.enums import AgentResultStatus, MissionStatus, TaskStatus, UserRole, VerificationPhase, VerificationStatus
from app.schemas.evidence import Evidence
from app.schemas.mission import MissionPlan, MissionTask
from app.schemas.verification import VerificationResult
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
        self._graph = self._build_graph()

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
            "errors": [],
            "final_result": prior_final_result,
        }
        return self._graph.invoke(state, config={"configurable": {"thread_id": mission_id}, "recursion_limit": GRAPH_RECURSION_LIMIT})

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
                metadata={"provider": getattr(self._llm_provider, "name", "unknown")},
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
            context.update_mission_status(state["mission_id"], MissionStatus.IN_PROGRESS)
        finally:
            session.close()
        return {"validation_errors": [], "mission_status": MissionStatus.IN_PROGRESS}

    def _schedule_ready_tasks(self, state: OrchestratorState) -> dict:
        plan = state["plan"]
        assert plan is not None
        ready, updated_status = compute_ready_and_blocked(plan, state.get("task_status", {}))
        return {"task_status": updated_status, "ready_task_ids": ready}

    def _dispatch_and_collect(self, state: OrchestratorState) -> dict:
        plan = state["plan"]
        assert plan is not None
        ready = state.get("ready_task_ids", [])

        base_facts: dict = {"student_id": state["student_id"]}
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

        return {
            "plan": new_plan,
            "task_status": task_status,
            "replan_count": state["replan_count"] + 1,
            "round_failures": {},
        }

    def _finalize(self, state: OrchestratorState) -> dict:
        final_text = self._summarize(state)
        session = self._session_factory()
        try:
            context = ContextService(session)
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
    def _summarize(state: OrchestratorState) -> str:
        plan = state.get("plan")
        responses = state.get("responses", {})
        unsupported = list(plan.unsupported_requests) if plan is not None else []
        # Iterate the plan's declared task order (not a lexical sort of task
        # ids) so a synthesized multi-agent result reads in the mission's
        # actual sequence -- matters once task ids exceed one digit, and is
        # the more correct ordering either way.
        ordered_task_ids = [task.task_id for task in plan.tasks] if plan is not None else sorted(responses)
        # Skip a task's answer when an earlier task's answer already contains
        # it verbatim (e.g. an eligibility answer that already states the
        # recovery path), so the synthesis doesn't repeat itself.
        parts: List[str] = []
        for task_id in ordered_task_ids:
            answer = (responses.get(task_id) or "").strip()
            if answer and not any(answer in earlier for earlier in parts):
                parts.append(answer)
        gathered = " ".join(parts)

        if state.get("planning_error"):
            text = f"Mission failed: the planner could not produce a usable plan ({state['planning_error']})."
            return f"{text} Results gathered before the failure: {gathered}" if gathered else f"{text} No task was executed."
        if state.get("validation_errors"):
            if plan is not None and not plan.tasks and unsupported:
                return "CampusNexus can't help with this goal: " + " ".join(unsupported)
            return "Mission failed: plan validation errors -- " + "; ".join(state["validation_errors"])

        text = gathered or f"Mission ended with status {state['mission_status'].value}."
        if state.get("duplicate_failure_stop"):
            text = f"{DUPLICATE_FAILURE_MESSAGE} {gathered}".strip()
        if unsupported:
            text += " Not handled by this mission: " + " ".join(unsupported)
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
