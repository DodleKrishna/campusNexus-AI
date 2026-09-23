"""Deterministic plan validation (CLAUDE.md: never execute an unvalidated plan).

Plain Python, no LLM -- everything here is checkable without asking a model
"is this plan okay?" a second time. Mirrors the shape of
app/verification/academic.py: independently-testable named checks producing
a clear pass/fail plus human-readable reasons, run once before any task in
the plan is ever dispatched.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List

from pydantic import BaseModel, Field

from app.graph.registry import AgentRegistry
from app.schemas.mission import MissionPlan

_MIN_OBJECTIVE_LENGTH = 3


class PlanValidationResult(BaseModel):
    """Outcome of validating a MissionPlan before any task is dispatched."""

    is_valid: bool
    errors: List[str] = Field(default_factory=list)


def validate_plan(plan: MissionPlan, registry: AgentRegistry, *, expected_mission_id: str) -> PlanValidationResult:
    """Check ``plan`` deterministically. Does not execute or dispatch anything.

    Note on "no self-dependencies": ``MissionTask`` (app/schemas/mission.py)
    already rejects ``task_id in dependencies`` at construction time via its
    own Pydantic validator, so no constructed ``MissionTask`` can ever
    exhibit one -- re-checking it here would be dead code, not defense in
    depth. Every other check below (uniqueness, dependency references,
    cycles, agent assignments, mission id, objectives) is *not* already
    guaranteed by the Phase 1 schemas and is checked explicitly.
    """
    errors: List[str] = []

    if plan.mission_id != expected_mission_id:
        errors.append(
            f"Plan mission_id {plan.mission_id!r} does not match the mission being planned "
            f"({expected_mission_id!r})."
        )

    if not plan.tasks:
        errors.append("Plan has no tasks.")
        return PlanValidationResult(is_valid=False, errors=errors)

    task_ids = [task.task_id for task in plan.tasks]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for task_id in task_ids:
        if task_id in seen:
            duplicates.add(task_id)
        seen.add(task_id)
    if duplicates:
        errors.append(f"Duplicate task ids: {sorted(duplicates)}")

    valid_task_ids = set(task_ids)

    for task in plan.tasks:
        if not registry.is_supported(task.agent):
            errors.append(
                f"Task {task.task_id!r} is assigned to unsupported agent {task.agent.value!r}; "
                f"supported: {[a.value for a in registry.supported_agents()]}"
            )
        if len(task.objective.strip()) < _MIN_OBJECTIVE_LENGTH:
            errors.append(f"Task {task.task_id!r} has an unsupported (too short) objective.")
        for dep in task.dependencies:
            if dep not in valid_task_ids:
                errors.append(f"Task {task.task_id!r} depends on unknown task id {dep!r}.")

    # Cycle detection + unreachable-task detection via Kahn's algorithm, over
    # only the *valid* dependency edges (a dangling reference is already
    # reported above; it shouldn't also be misreported as part of a cycle).
    indegree: Dict[str, int] = {task_id: 0 for task_id in task_ids}
    dependents: Dict[str, List[str]] = {task_id: [] for task_id in task_ids}
    for task in plan.tasks:
        for dep in task.dependencies:
            if dep in valid_task_ids:
                indegree[task.task_id] += 1
                dependents[dep].append(task.task_id)

    queue = deque(task_id for task_id, degree in indegree.items() if degree == 0)
    ordered: List[str] = []
    remaining = dict(indegree)
    while queue:
        current = queue.popleft()
        ordered.append(current)
        for dependent in dependents[current]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                queue.append(dependent)

    if len(ordered) != len(task_ids):
        unreachable = sorted(set(task_ids) - set(ordered))
        errors.append(f"Circular or unreachable task dependencies detected among: {unreachable}")

    return PlanValidationResult(is_valid=not errors, errors=errors)
