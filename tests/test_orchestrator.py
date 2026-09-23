"""Orchestration evaluation: app/graph/orchestrator.py end-to-end, using
deterministic test-double agents (tests/graph_doubles.py) -- no live LLM,
no network, and no dependency on the real (read-only) Academic Agent, so
DAG shapes (dependencies, parallelism, failure, retry) can be constructed
freely per scenario.
"""
from __future__ import annotations

import threading
import time

from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.schemas.enums import AgentName, MissionStatus, TaskStatus, UserRole, VerificationStatus
from app.schemas.mission import MissionPlan, MissionTask
from tests.graph_doubles import (
    AlwaysFailAgent,
    FixedPlanLLMProvider,
    MismatchedVerificationAgent,
    NeedsReviewAgent,
    SuccessAgent,
)

MISSION_USER = "STU-DEMO-001"


def _task(mission_id: str, index: int, agent: AgentName, dependencies=None) -> MissionTask:
    return MissionTask(
        task_id=f"{mission_id}-task-{index}",
        mission_id=mission_id,
        agent=agent,
        objective=f"synthetic task {index}",
        dependencies=dependencies or [],
    )


def _registry(agents: dict) -> AgentRegistry:
    registry = AgentRegistry()
    for name, factory in agents.items():
        registry.register(name, factory)
    return registry


def _orchestrator(session_factory, registry, plan_factory, max_replans=2) -> MissionOrchestrator:
    llm_provider = FixedPlanLLMProvider(plan_factory=plan_factory)
    return MissionOrchestrator(
        session_factory=session_factory, registry=registry, llm_provider=llm_provider, max_replans=max_replans
    )


def test_valid_single_agent_mission_completes(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[_task(mission_id, 1, AgentName.ACADEMIC_AGENT)])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: SuccessAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory)

    final = orchestrator.run_mission("do one thing", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.COMPLETED
    assert len(final["task_status"]) == 1
    assert all(status == TaskStatus.COMPLETED for status in final["task_status"].values())


def test_valid_multi_task_mission_with_dependency_completes_in_order(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        a = _task(mission_id, 1, AgentName.ACADEMIC_AGENT)
        b = _task(mission_id, 2, AgentName.ACADEMIC_AGENT, dependencies=[a.task_id])
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[a, b])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: SuccessAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory)

    final = orchestrator.run_mission("two step mission", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.COMPLETED
    assert all(status == TaskStatus.COMPLETED for status in final["task_status"].values())


def test_parallel_independent_tasks_run_concurrently(session_factory) -> None:
    """Proves genuine concurrent dispatch via observed peak overlap, not wall-clock
    timing -- a wall-clock bound on the whole orchestrator round-trip would also be
    sensitive to legitimate sequential Context Service bookkeeping (each mission/step/
    audit write is its own committed transaction), which is unrelated to whether
    *dispatch* itself is parallel."""
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    class TrackedSuccessAgent(SuccessAgent):
        def handle(self, message):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1
            return super().handle(message)

    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        tasks = [_task(mission_id, i, AgentName.ACADEMIC_AGENT) for i in range(1, 4)]
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=tasks)

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: TrackedSuccessAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory)

    final = orchestrator.run_mission("three independent things", user_id=MISSION_USER, user_role=UserRole.STUDENT)

    assert final["mission_status"] == MissionStatus.COMPLETED
    assert state["peak"] > 1, "independent tasks were dispatched serially, not in parallel"


def test_invalid_agent_assignment_fails_before_dispatch(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[_task(mission_id, 1, AgentName.CAREER_AGENT)])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: SuccessAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory)

    final = orchestrator.run_mission("unsupported agent", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.FAILED
    assert final["validation_errors"]
    assert final["task_status"] == {}  # never dispatched


def test_missing_dependency_fails_before_dispatch(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        task = _task(mission_id, 1, AgentName.ACADEMIC_AGENT, dependencies=["does-not-exist"])
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[task])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: SuccessAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory)

    final = orchestrator.run_mission("dangling dependency", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.FAILED
    assert any("unknown task id" in e.lower() for e in final["validation_errors"])


def test_circular_dependency_fails_before_dispatch(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        a = MissionTask(
            task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT,
            objective="task a", dependencies=[f"{mission_id}-task-2"],
        )
        b = MissionTask(
            task_id=f"{mission_id}-task-2", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT,
            objective="task b", dependencies=[f"{mission_id}-task-1"],
        )
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[a, b])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: SuccessAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory)

    final = orchestrator.run_mission("circular", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.FAILED
    assert any("circular" in e.lower() for e in final["validation_errors"])


def test_agent_failure_blocks_dependents_without_running_them(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        a = _task(mission_id, 1, AgentName.ACADEMIC_AGENT)
        b = _task(mission_id, 2, AgentName.ACADEMIC_AGENT, dependencies=[a.task_id])
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[a, b])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: AlwaysFailAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory, max_replans=0)

    final = orchestrator.run_mission("upstream fails", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    task_a, task_b = (f"{final['mission_id']}-task-1", f"{final['mission_id']}-task-2")
    assert final["task_status"][task_a] == TaskStatus.FAILED
    assert final["task_status"][task_b] == TaskStatus.SKIPPED  # never dispatched
    assert final["mission_status"] == MissionStatus.FAILED


def test_successful_invocation_is_not_treated_as_verified(session_factory) -> None:
    """A no-exception agent call with AgentResultStatus.SUCCESS but a FAILED
    VerificationResult must still fail the mission -- the Orchestrator routes
    on verification.status, never on "the call didn't raise"."""

    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[_task(mission_id, 1, AgentName.ACADEMIC_AGENT)])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: MismatchedVerificationAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory, max_replans=0)

    final = orchestrator.run_mission("looks fine but isn't", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.FAILED
    task_id = f"{final['mission_id']}-task-1"
    assert final["task_status"][task_id] == TaskStatus.FAILED
    assert final["agent_results"][task_id].status.value == "success"  # the call itself "succeeded"
    assert final["verifications"][task_id].status == VerificationStatus.FAILED  # but was never verified


def test_needs_review_pauses_the_mission_without_auto_approving(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[_task(mission_id, 1, AgentName.ACADEMIC_AGENT)])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: NeedsReviewAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory)

    final = orchestrator.run_mission("ambiguous thing", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.NEEDS_APPROVAL
    assert all(status == TaskStatus.BLOCKED for status in final["task_status"].values())
    # Never silently treated as done.
    assert final["mission_status"] != MissionStatus.COMPLETED


def test_retry_limit_is_respected_not_looped_forever(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[_task(mission_id, 1, AgentName.ACADEMIC_AGENT)])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: AlwaysFailAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory, max_replans=2)

    final = orchestrator.run_mission("always fails", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.FAILED
    assert final["replan_count"] == 2  # bounded, not unlimited


def test_zero_replans_fails_immediately_on_first_failure(session_factory) -> None:
    def plan_factory(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[_task(mission_id, 1, AgentName.ACADEMIC_AGENT)])

    registry = _registry({AgentName.ACADEMIC_AGENT: lambda session: AlwaysFailAgent(session)})
    orchestrator = _orchestrator(session_factory, registry, plan_factory, max_replans=0)

    final = orchestrator.run_mission("fails once", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.FAILED
    assert final["replan_count"] == 0
