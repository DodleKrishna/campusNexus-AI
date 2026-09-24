"""Action Agent proposal + write-tool input schemas (Phase 7).

``ActionProposal`` is the Action Agent's own draft of a write action before
Verifier pre-check -- it carries the "why" (supporting facts, evidence refs, a
user-visible description) that ``app.schemas.tools.ToolCall`` deliberately
doesn't, so those fields are added here rather than duplicated onto
``ToolCall``. Once a proposal clears pre-action verification, the Action
Agent builds the actual ``ToolCall`` (idempotency key + validated arguments)
from it -- see ``app/agents/action/agent.py``.

The three ``*Input`` models are each write tool's ``ToolDefinition.input_model``
(``app/tools/registry.py``): the only shape a ``ToolCall.arguments`` dict is
ever validated against before a handler runs, so no tool ever executes an
unvalidated raw dict.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import JsonValue, NonBlankStr, utc_now


class TargetSource(str, Enum):
    """Where an action's target resource came from (Phase 12B).

    Only a target the student named -- in the mission goal, or directly by the
    caller of an explicit edit -- may reach a proposal. An agent's
    recommendation ("best match", "first result") is never a source.
    """

    USER_GOAL = "user_goal"
    CALLER_SUPPLIED = "caller_supplied"
    NOT_REQUIRED = "not_required"
    UNCONFIRMED = "unconfirmed"


class TargetProvenance(BaseModel):
    """Deterministic verdict on whether an action's target was explicitly selected."""

    tool_name: str
    target_field: Optional[str] = None
    target_value: Optional[str] = None
    source: TargetSource
    reason: Optional[str] = None

    @property
    def confirmed(self) -> bool:
        return self.source != TargetSource.UNCONFIRMED


class ActionProposal(BaseModel):
    """A draft write action, not yet pre-checked or approved."""

    proposal_id: NonBlankStr
    mission_id: NonBlankStr
    task_id: NonBlankStr
    tool_name: NonBlankStr
    target_resource: NonBlankStr
    student_id: NonBlankStr
    parameters: Dict[str, JsonValue] = Field(default_factory=dict)
    supporting_facts: Dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: List[str] = Field(default_factory=list)
    description: NonBlankStr
    created_at: datetime = Field(default_factory=utc_now)


class RegisterEventInput(BaseModel):
    """Validated arguments for the ``register_event`` write tool."""

    student_id: NonBlankStr
    event_id: int = Field(gt=0)


class CreateCalendarEventInput(BaseModel):
    """Validated arguments for the ``create_calendar_event`` write tool."""

    student_id: NonBlankStr
    title: NonBlankStr
    start_at: datetime
    end_at: datetime
    source_type: NonBlankStr = "personal"
    source_id: Optional[int] = None

    @model_validator(mode="after")
    def _end_after_start(self) -> "CreateCalendarEventInput":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class CreateCampusCaseInput(BaseModel):
    """Validated arguments for the ``create_campus_case`` write tool."""

    student_id: NonBlankStr
    category: NonBlankStr
    description: NonBlankStr
    priority: NonBlankStr = "normal"
    department: NonBlankStr
