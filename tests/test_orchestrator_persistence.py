"""Persistence + interruption/resumption tests for the Mission Orchestrator.

Uses the existing Context Service (app/services/context.py) as the ground
truth throughout -- these tests exist specifically to prove the claims in
docs/ARCHITECTURE.md's "Mission Orchestrator" section: the Context Service,
not the (process-memory-only) LangGraph checkpointer, is what makes a
mission resumable after a real interruption.
"""
from __future__ import annotations

import pytest

from app.db.base import utc_now
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.schemas.enums import AgentName, AgentResultStatus, MissionStatus, TaskStatus, UserRole
from app.schemas.mission import MissionPlan, MissionTask
from app.services.context import ContextService
from tests.graph_doubles import FixedPlanLLMProvider, SuccessAgent

MISSION_USER = "STU-DEMO-001"


def _task(mission_id: str, index: int, dependencies=None) -> MissionTask:
    return MissionTask(
        task_id=f"{mission_id}-task-{index}",
        mission_id=mission_id,
        agent=AgentName.ACADEMIC_AGENT,
        objective=f"synthetic task {index}",
        dependencies=dependencies or [],
    )


def _orchestrator(session_factory, registry, plan_factory, max_replans=2) -> MissionOrchestrator:
    llm_provider = FixedPlanLLMProvider(plan_factory=plan_factory)
    return MissionOrchestrator(
        session_factory=session_factory, registry=registry, llm_provider=llm_provider, max_replans=max_replans
    )


def test_full_run_persists_mission_step_agent_run_and_audit_trail(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[_task(mission_id, 1)])

    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda session: SuccessAgent(session))
    orchestrator = _orchestrator(session_factory, registry, plan_factory)

    final = orchestrator.run_mission("one thing", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    mission_id = final["mission_id"]
    task_id = f"{mission_id}-task-1"

    with session_factory() as session:
        context = ContextService(session)

        mission = context.get_mission(mission_id)
        assert mission is not None
        assert mission.status == MissionStatus.COMPLETED
        assert mission.final_result

        steps = {step.step_id: step for step in mission.steps}
        assert steps[task_id].status == TaskStatus.COMPLETED

        runs = context.list_agent_runs(mission_id)
        assert len(runs) == 1
        assert runs[0].status == AgentResultStatus.SUCCESS

        snapshot = context.get_latest_plan_snapshot(mission_id)
        assert snapshot is not None
        assert [t.task_id for t in snapshot.tasks] == [task_id]

        event_types = [e.event_type for e in context.list_audit_events(mission_id)]
        for expected in ("mission_created", "plan_generated", "task_verified", "mission_finalized"):
            assert expected in event_types, f"missing audit event type {expected!r} in {event_types}"


def test_resume_unknown_mission_raises(session_factory) -> None:
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda session: SuccessAgent(session))
    orchestrator = _orchestrator(session_factory, registry, plan_factory=lambda mid, goal: None)

    with pytest.raises(ValueError, match="unknown mission_id"):
        orchestrator.resume_mission("mission-does-not-exist")


def test_resume_mission_without_plan_snapshot_raises(session_factory) -> None:
    with session_factory() as session:
        ContextService(session).create_mission("mission-no-plan", MISSION_USER, UserRole.STUDENT, "goal")

    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda session: SuccessAgent(session))
    orchestrator = _orchestrator(session_factory, registry, plan_factory=lambda mid, goal: None)

    with pytest.raises(ValueError, match="no persisted plan"):
        orchestrator.resume_mission("mission-no-plan")


def test_resume_completes_remaining_work_without_redispatching_completed_tasks(session_factory) -> None:
    """Simulates a process interruption after task A completed but before task B ran.

    Built directly via the Context Service (the durable source of truth)
    rather than by running the orchestrator and stopping it partway --
    run_mission always drives a mission to a terminal/paused state in one
    call, so this is the honest way to construct a genuinely-interrupted
    mid-DAG state without process-kill semantics in a test.
    """
    mission_id = "mission-manual-resume"
    task_a = _task(mission_id, 1)
    task_b = _task(mission_id, 2, dependencies=[task_a.task_id])
    plan = MissionPlan(mission_id=mission_id, goal="two step mission", tasks=[task_a, task_b])

    with session_factory() as session:
        context = ContextService(session)
        context.create_mission(mission_id, MISSION_USER, UserRole.STUDENT, plan.goal)
        context.append_audit_event(
            event_id="evt-setup-plan",
            mission_id=mission_id,
            event_type="plan_generated",
            actor="test-setup",
            message="plan",
            metadata={"plan": plan.model_dump(mode="json")},
        )
        context.create_mission_step(
            step_id=task_a.task_id, mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT,
            objective=task_a.objective, sequence=0,
        )
        context.create_mission_step(
            step_id=task_b.task_id, mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT,
            objective=task_b.objective, sequence=1,
        )
        context.record_agent_result(
            mission_id=mission_id, step_id=task_a.task_id, agent=AgentName.ACADEMIC_AGENT,
            status=AgentResultStatus.SUCCESS, facts={"objective": task_a.objective}, errors=[],
        )
        context.update_mission_step(task_a.task_id, status=TaskStatus.COMPLETED, completed_at=utc_now())
        context.update_mission_status(mission_id, MissionStatus.IN_PROGRESS)

    class RedispatchGuardAgent(SuccessAgent):
        def handle(self, message):
            if message.task_id == task_a.task_id:
                raise AssertionError("an already-completed task must never be re-dispatched on resume")
            return super().handle(message)

    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda session: RedispatchGuardAgent(session))

    def _never_call(mission_id: str, goal: str) -> MissionPlan:
        raise AssertionError("plan_mission must not be called on resume -- the plan comes from the audit snapshot")

    orchestrator = _orchestrator(session_factory, registry, plan_factory=_never_call)

    final = orchestrator.resume_mission(mission_id)

    assert final["mission_status"] == MissionStatus.COMPLETED
    assert final["task_status"][task_a.task_id] == TaskStatus.COMPLETED
    assert final["task_status"][task_b.task_id] == TaskStatus.COMPLETED

    with session_factory() as session:
        context = ContextService(session)
        mission = context.get_mission(mission_id)
        assert mission.status == MissionStatus.COMPLETED
        runs = context.list_agent_runs(mission_id)
        # Exactly one run for task A (from setup) and one for task B (from resume).
        assert len(runs) == 2
        event_types = [e.event_type for e in context.list_audit_events(mission_id)]
        assert "mission_resumed" in event_types


def test_resume_of_already_completed_mission_is_a_safe_no_op(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[_task(mission_id, 1)])

    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda session: SuccessAgent(session))
    orchestrator = _orchestrator(session_factory, registry, plan_factory)

    first = orchestrator.run_mission("one thing", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    second = orchestrator.resume_mission(first["mission_id"])

    assert second["mission_status"] == MissionStatus.COMPLETED
    assert second["task_status"] == first["task_status"]
