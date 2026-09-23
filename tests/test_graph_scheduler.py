"""Unit tests for the dependency-aware DAG scheduler (app/graph/scheduler.py)."""
from __future__ import annotations

from app.graph.scheduler import compute_ready_and_blocked, has_blocked_tasks, has_failed_tasks, is_mission_complete
from app.schemas.enums import AgentName, TaskStatus
from app.schemas.mission import MissionPlan, MissionTask

MISSION_ID = "mission-1"


def _task(task_id: str, dependencies: list[str] | None = None) -> MissionTask:
    return MissionTask(
        task_id=task_id,
        mission_id=MISSION_ID,
        agent=AgentName.ACADEMIC_AGENT,
        objective=f"objective for {task_id}",
        dependencies=dependencies or [],
    )


def test_independent_tasks_are_all_ready_together() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a"), _task("b"), _task("c")])
    ready, status = compute_ready_and_blocked(plan, {})
    assert set(ready) == {"a", "b", "c"}
    assert all(status[t] == TaskStatus.PENDING for t in ("a", "b", "c"))


def test_dependent_task_waits_for_prerequisite() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a"), _task("b", ["a"])])
    ready, _ = compute_ready_and_blocked(plan, {})
    assert ready == ["a"]

    ready2, status2 = compute_ready_and_blocked(plan, {"a": TaskStatus.COMPLETED})
    assert ready2 == ["b"]
    assert status2["a"] == TaskStatus.COMPLETED


def test_failed_prerequisite_cascades_to_skipped_not_ready() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a"), _task("b", ["a"]), _task("c", ["b"])])
    ready, status = compute_ready_and_blocked(plan, {"a": TaskStatus.FAILED})
    assert ready == []
    assert status["b"] == TaskStatus.SKIPPED
    assert status["c"] == TaskStatus.SKIPPED  # cascades transitively


def test_blocked_prerequisite_is_not_skipped_dependents_just_wait() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a"), _task("b", ["a"])])
    ready, status = compute_ready_and_blocked(plan, {"a": TaskStatus.BLOCKED})
    assert ready == []
    assert status["b"] == TaskStatus.PENDING  # a pause is not a failure


def test_diamond_dependency_resolves_once_both_branches_complete() -> None:
    # a -> {b, c} -> d
    plan = MissionPlan(
        mission_id=MISSION_ID,
        goal="g",
        tasks=[_task("a"), _task("b", ["a"]), _task("c", ["a"]), _task("d", ["b", "c"])],
    )
    ready, status = compute_ready_and_blocked(plan, {"a": TaskStatus.COMPLETED, "b": TaskStatus.COMPLETED})
    assert ready == ["c"]
    assert status["d"] == TaskStatus.PENDING

    ready2, _ = compute_ready_and_blocked(
        plan, {"a": TaskStatus.COMPLETED, "b": TaskStatus.COMPLETED, "c": TaskStatus.COMPLETED}
    )
    assert ready2 == ["d"]


def test_is_mission_complete() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a"), _task("b")])
    assert not is_mission_complete(plan, {"a": TaskStatus.COMPLETED})
    assert is_mission_complete(plan, {"a": TaskStatus.COMPLETED, "b": TaskStatus.SKIPPED})
    assert is_mission_complete(plan, {"a": TaskStatus.COMPLETED, "b": TaskStatus.FAILED})


def test_has_blocked_and_failed_predicates() -> None:
    assert has_blocked_tasks({"a": TaskStatus.BLOCKED})
    assert not has_blocked_tasks({"a": TaskStatus.COMPLETED})
    assert has_failed_tasks({"a": TaskStatus.FAILED})
    assert not has_failed_tasks({"a": TaskStatus.SKIPPED})
