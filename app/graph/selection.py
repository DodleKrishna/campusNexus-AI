"""Phase 13: deterministic continuation of a mission after the student selects a target.

Pure functions, no I/O and no LLM. The Orchestrator uses them to turn the
current plan plus one server-validated ``SelectedTarget`` into the next plan
version of the *same* mission: the action task for that target (reusing the
step an earlier selection or an unconfirmed action task already occupies, so
a changed selection never leaves a second action behind), wired to the
verified schedule tasks and the Events task that recommended the target.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Mapping, Optional

from app.schemas.enums import AgentName
from app.schemas.mission import MissionPlan, MissionTask
from app.schemas.selection import SelectedTarget

_TASK_INDEX_RE = re.compile(r"-task-(\d+)$")


def _next_task_id(mission_id: str, existing_ids: Iterable[str]) -> str:
    indices = [int(m.group(1)) for task_id in existing_ids if (m := _TASK_INDEX_RE.search(task_id))]
    return f"{mission_id}-task-{max(indices, default=0) + 1}"


def continuation_step_id(
    plan: MissionPlan,
    *,
    tool_name: str,
    selections: Mapping[str, SelectedTarget],
    unconfirmed_task_ids: Iterable[str],
    existing_step_ids: Iterable[str],
) -> str:
    """The step the selected action runs under.

    1. The step of the current selection for this tool (a changed selection
       replaces the action in place -- one action step per selection slot).
    2. An action task for this tool whose target was never confirmed.
    3. Otherwise a new task id that no step of this mission has used yet.
    """
    for step_id, selection in selections.items():
        if selection.tool_name == tool_name:
            return step_id
    unconfirmed = set(unconfirmed_task_ids)
    for task in plan.tasks:
        if task.task_id in unconfirmed and task.constraints.get("tool_name") == tool_name:
            return task.task_id
    return _next_task_id(plan.mission_id, [*(t.task_id for t in plan.tasks), *existing_step_ids])


def build_selection_continuation_plan(
    plan: MissionPlan,
    *,
    step_id: str,
    selection: SelectedTarget,
    upstream_step_ids: List[str],
) -> MissionPlan:
    """The next plan version: ``plan`` plus the action for ``selection``.

    The action's constraints name the canonical resource (real id + title
    from the database); the student's selection record -- not these
    constraints -- is what authorizes it. The tool is removed from
    ``selection_required_actions`` because the student has now chosen.
    """
    known = {task.task_id for task in plan.tasks}
    existing: Optional[MissionTask] = next((t for t in plan.tasks if t.task_id == step_id), None)
    dependencies = [d for d in dict.fromkeys([*(existing.dependencies if existing else []), *upstream_step_ids]) if d in known and d != step_id]
    constraints = {"tool_name": selection.tool_name, "event_id": selection.resource_id, "event_title": selection.title}
    objective = f"Register the student for the event they selected: '{selection.title}'."
    if existing is not None:
        action = existing.model_copy(
            update={"agent": AgentName.ACTION_AGENT, "objective": objective, "dependencies": dependencies, "constraints": constraints}
        )
        tasks = [action if t.task_id == step_id else t for t in plan.tasks]
    else:
        action = MissionTask(
            task_id=step_id, mission_id=plan.mission_id, agent=AgentName.ACTION_AGENT, objective=objective,
            dependencies=dependencies, constraints=constraints, requires_evidence=True,
        )
        tasks = [*plan.tasks, action]
    return plan.model_copy(
        update={
            "tasks": tasks,
            "selection_required_actions": [a for a in plan.selection_required_actions if a != selection.tool_name],
            "version": plan.version + 1,
        }
    )
