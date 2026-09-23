"""Tests for mission planning: app/graph/planner.py + MockLLMProvider.plan_mission."""
from __future__ import annotations

from app.graph.planner import generate_plan
from app.llm.providers.mock import MockLLMProvider
from app.schemas.enums import AgentName

provider = MockLLMProvider()


def test_generate_plan_delegates_to_the_configured_provider() -> None:
    plan = generate_plan(provider, "mission-1", "Check my OS attendance.", supported_agents=[AgentName.ACADEMIC_AGENT])
    assert plan.mission_id == "mission-1"
    assert len(plan.tasks) == 1
    assert plan.tasks[0].agent == AgentName.ACADEMIC_AGENT


def test_compound_goal_decomposes_into_independent_tasks() -> None:
    goal = (
        "Check my Operating Systems attendance, determine whether I currently meet the attendance "
        "requirement, and explain how many classes I need to attend to reach the required attendance."
    )
    plan = provider.plan_mission("mission-2", goal, supported_agents=[AgentName.ACADEMIC_AGENT])
    assert len(plan.tasks) == 3
    assert all(task.dependencies == [] for task in plan.tasks)  # independent -> parallel-eligible
    assert {task.task_id for task in plan.tasks} == {
        "mission-2-task-1",
        "mission-2-task-2",
        "mission-2-task-3",
    }


def test_shared_subject_is_carried_into_clauses_that_omit_it() -> None:
    goal = "Check my Operating Systems attendance and explain how many classes I need to attend."
    plan = provider.plan_mission("mission-3", goal, supported_agents=[AgentName.ACADEMIC_AGENT])
    assert "Operating Systems" in plan.tasks[0].objective
    assert "Operating Systems" in plan.tasks[1].objective  # carried forward from clause 1


def test_single_clause_goal_produces_one_task() -> None:
    plan = provider.plan_mission("mission-4", "What's my attendance in OS?", supported_agents=[AgentName.ACADEMIC_AGENT])
    assert len(plan.tasks) == 1
    assert plan.tasks[0].objective == "What's my attendance in OS?"


def test_all_tasks_require_evidence_and_target_a_supported_agent() -> None:
    plan = provider.plan_mission("mission-5", "Check my OS attendance.", supported_agents=[AgentName.ACADEMIC_AGENT])
    assert all(task.requires_evidence for task in plan.tasks)
    assert all(task.agent == AgentName.ACADEMIC_AGENT for task in plan.tasks)


def test_plan_is_reproducible_for_the_same_goal_and_mission_id() -> None:
    goal = "Check my Operating Systems attendance and explain how many classes I need to attend."
    plan_a = provider.plan_mission("mission-6", goal, supported_agents=[AgentName.ACADEMIC_AGENT])
    plan_b = provider.plan_mission("mission-6", goal, supported_agents=[AgentName.ACADEMIC_AGENT])
    assert [t.task_id for t in plan_a.tasks] == [t.task_id for t in plan_b.tasks]
    assert [t.objective for t in plan_a.tasks] == [t.objective for t in plan_b.tasks]
