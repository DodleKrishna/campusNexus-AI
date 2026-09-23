"""Dependency-aware DAG scheduling.

Pure functions over a ``MissionPlan`` + the current per-task ``TaskStatus``
map -- no I/O, no agent calls, fully unit-testable in isolation from
LangGraph. A failed (or already-skipped) prerequisite cascades to
``SKIPPED`` for every transitive dependent, so a failed prerequisite can
never silently let a dependent task run. A ``BLOCKED`` (needs-review)
prerequisite is different -- it's a pause, not a failure -- so its
dependents simply never become ready while it stays blocked; they are not
skipped.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from app.schemas.enums import TaskStatus
from app.schemas.mission import MissionPlan

_CASCADES_AS_SKIPPED = {TaskStatus.FAILED, TaskStatus.SKIPPED}
_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}


def compute_ready_and_blocked(
    plan: MissionPlan, task_status: Dict[str, TaskStatus]
) -> Tuple[List[str], Dict[str, TaskStatus]]:
    """Return ``(ready_task_ids, updated_task_status)``.

    ``ready_task_ids`` are ``PENDING`` tasks whose every dependency is
    ``COMPLETED``. ``updated_task_status`` cascades ``SKIPPED`` to any
    ``PENDING`` task with a ``FAILED``/``SKIPPED`` dependency, repeated to a
    fixed point so the cascade propagates through chains of dependents.
    """
    updated = dict(task_status)
    for task in plan.tasks:
        updated.setdefault(task.task_id, TaskStatus.PENDING)

    changed = True
    while changed:
        changed = False
        for task in plan.tasks:
            if updated[task.task_id] != TaskStatus.PENDING:
                continue
            dep_statuses = [updated[dep] for dep in task.dependencies if dep in updated]
            if any(status in _CASCADES_AS_SKIPPED for status in dep_statuses):
                updated[task.task_id] = TaskStatus.SKIPPED
                changed = True

    ready = [
        task.task_id
        for task in plan.tasks
        if updated[task.task_id] == TaskStatus.PENDING
        and all(updated.get(dep) == TaskStatus.COMPLETED for dep in task.dependencies)
    ]
    return ready, updated


def is_mission_complete(plan: MissionPlan, task_status: Dict[str, TaskStatus]) -> bool:
    """True once every task has reached a terminal status (COMPLETED/FAILED/SKIPPED)."""
    return all(task_status.get(task.task_id) in _TERMINAL for task in plan.tasks)


def has_blocked_tasks(task_status: Dict[str, TaskStatus]) -> bool:
    """True if any task is BLOCKED (a NEEDS_REVIEW verification pausing the mission)."""
    return any(status == TaskStatus.BLOCKED for status in task_status.values())


def has_failed_tasks(task_status: Dict[str, TaskStatus]) -> bool:
    return any(status == TaskStatus.FAILED for status in task_status.values())
