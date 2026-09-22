"""Agent-to-agent/orchestrator structured message and result schemas.

Every agent handoff uses these Pydantic models as the structured-output
contract (CLAUDE.md Structured Output Requirement) -- no free-form prose
crosses a component boundary.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import JsonValue, NonBlankStr, utc_now
from app.schemas.enums import AgentName, AgentResultStatus
from app.schemas.evidence import Evidence
from app.schemas.tools import ToolCall


class AgentMessage(BaseModel):
    """A structured request dispatched from the Orchestrator to a specialist agent
    (or, in future phases, between two agents via the Orchestrator)."""

    message_id: NonBlankStr
    mission_id: NonBlankStr
    task_id: NonBlankStr
    source: AgentName
    target: AgentName
    objective: NonBlankStr
    facts: Dict[str, JsonValue] = Field(default_factory=dict)
    constraints: Dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: List[str] = Field(default_factory=list)
    context_refs: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class AgentResult(BaseModel):
    """The structured output an agent hands back to the Orchestrator for a task.

    proposed_actions are draft ToolCalls the agent wants performed; only the
    Action Agent may actually execute them, and only after the Verifier and
    (if sensitive) the Approval Gate have passed them.
    """

    mission_id: NonBlankStr
    task_id: NonBlankStr
    agent: AgentName
    status: AgentResultStatus
    facts: Dict[str, JsonValue] = Field(default_factory=dict)
    evidence: List[Evidence] = Field(default_factory=list)
    proposed_actions: List[ToolCall] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
