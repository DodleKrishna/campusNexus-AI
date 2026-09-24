"""Mission plan and task schemas produced by the Mission Orchestrator.

MissionTask.dependencies holds other task_ids and is deliberately a plain list
of ids (not embedded task objects) so the Orchestrator can build a DAG and
schedule/replan without duplicating task state.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import JsonValue, NonBlankStr, utc_now
from app.schemas.enums import AgentName, TaskStatus


class MissionTask(BaseModel):
    """A single step of a mission plan, dispatched to one specialist agent."""

    task_id: NonBlankStr
    mission_id: NonBlankStr
    agent: AgentName
    objective: NonBlankStr
    dependencies: List[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    context_refs: List[str] = Field(default_factory=list)
    constraints: Dict[str, JsonValue] = Field(default_factory=dict)
    requires_evidence: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "MissionTask":
        if self.task_id in self.dependencies:
            raise ValueError("a task cannot list itself as a dependency")
        return self

    @model_validator(mode="after")
    def _completed_at_requires_completed_status(self) -> "MissionTask":
        if self.completed_at is not None and self.status != TaskStatus.COMPLETED:
            raise ValueError("completed_at may only be set when status is COMPLETED")
        return self


class MissionPlan(BaseModel):
    """The decomposition of a student goal into a sequence of dependent tasks."""

    mission_id: NonBlankStr
    goal: NonBlankStr
    tasks: List[MissionTask] = Field(default_factory=list)
    # Phase 9 addition (additive): parts of the goal the planner could not map
    # onto any supported agent/tool, stated plainly rather than invented as a
    # task. A plan with no tasks and a non-empty list is a clear "can't help
    # with this" -- the validator still rejects it, so nothing is dispatched.
    unsupported_requests: List[NonBlankStr] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _tasks_belong_to_mission(self) -> "MissionPlan":
        for task in self.tasks:
            if task.mission_id != self.mission_id:
                raise ValueError(
                    "every MissionTask in a MissionPlan must share the plan's mission_id"
                )
        return self
