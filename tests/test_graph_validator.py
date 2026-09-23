"""Unit tests for the deterministic plan validator (app/graph/validator.py)."""
from __future__ import annotations

from app.graph.registry import AgentRegistry
from app.graph.validator import validate_plan
from app.schemas.enums import AgentName
from app.schemas.mission import MissionPlan, MissionTask

MISSION_ID = "mission-1"


def _task(task_id: str, agent: AgentName = AgentName.ACADEMIC_AGENT, dependencies=None, objective="a valid objective") -> MissionTask:
    return MissionTask(
        task_id=task_id, mission_id=MISSION_ID, agent=agent, objective=objective, dependencies=dependencies or []
    )


def _registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda session: None)
    return registry


def test_valid_plan_passes() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a"), _task("b", dependencies=["a"])])
    result = validate_plan(plan, _registry(), expected_mission_id=MISSION_ID)
    assert result.is_valid
    assert result.errors == []


def test_empty_plan_is_rejected() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[])
    result = validate_plan(plan, _registry(), expected_mission_id=MISSION_ID)
    assert not result.is_valid
    assert "no tasks" in result.errors[0].lower()


def test_mission_id_mismatch_is_rejected() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a")])
    result = validate_plan(plan, _registry(), expected_mission_id="a-different-mission")
    assert not result.is_valid
    assert any("mission_id" in e for e in result.errors)


def test_duplicate_task_ids_are_rejected() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a"), _task("a")])
    result = validate_plan(plan, _registry(), expected_mission_id=MISSION_ID)
    assert not result.is_valid
    assert any("Duplicate task ids" in e for e in result.errors)


def test_unsupported_agent_assignment_is_rejected() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a", agent=AgentName.CAREER_AGENT)])
    result = validate_plan(plan, _registry(), expected_mission_id=MISSION_ID)
    assert not result.is_valid
    assert any("unsupported agent" in e.lower() for e in result.errors)


def test_unknown_dependency_reference_is_rejected() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a", dependencies=["does-not-exist"])])
    result = validate_plan(plan, _registry(), expected_mission_id=MISSION_ID)
    assert not result.is_valid
    assert any("unknown task id" in e.lower() for e in result.errors)


def test_circular_dependency_is_rejected() -> None:
    plan = MissionPlan(
        mission_id=MISSION_ID,
        goal="g",
        tasks=[_task("a", dependencies=["b"]), _task("b", dependencies=["a"])],
    )
    result = validate_plan(plan, _registry(), expected_mission_id=MISSION_ID)
    assert not result.is_valid
    assert any("circular" in e.lower() for e in result.errors)


def test_task_downstream_of_a_cycle_is_reported_unreachable() -> None:
    # a <-> b is a cycle; c depends on b and can never become reachable.
    plan = MissionPlan(
        mission_id=MISSION_ID,
        goal="g",
        tasks=[_task("a", dependencies=["b"]), _task("b", dependencies=["a"]), _task("c", dependencies=["b"])],
    )
    result = validate_plan(plan, _registry(), expected_mission_id=MISSION_ID)
    assert not result.is_valid
    message = " ".join(result.errors)
    assert "'a'" in message and "'b'" in message and "'c'" in message


def test_too_short_objective_is_rejected() -> None:
    plan = MissionPlan(mission_id=MISSION_ID, goal="g", tasks=[_task("a", objective="ok")])
    result = validate_plan(plan, _registry(), expected_mission_id=MISSION_ID)
    assert not result.is_valid
    assert any("unsupported" in e.lower() and "objective" in e.lower() for e in result.errors)


def test_multiple_independent_errors_are_all_reported() -> None:
    plan = MissionPlan(
        mission_id=MISSION_ID,
        goal="g",
        tasks=[_task("a", agent=AgentName.CAREER_AGENT), _task("b", dependencies=["missing"])],
    )
    result = validate_plan(plan, _registry(), expected_mission_id=MISSION_ID)
    assert not result.is_valid
    assert len(result.errors) >= 2
