"""Shared mission state, audit trail, and error record schemas.

CampusState mirrors what the Context Service (a pure state store, not an
agent) persists: mission identity, the current plan, every agent result,
accumulated evidence, tool results, pending approvals, errors, and the audit
trail. No planning/reasoning logic lives here or in the Context Service.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.agent import AgentResult
from app.schemas.approval import ApprovalRequest
from app.schemas.common import JsonValue, NonBlankStr, utc_now
from app.schemas.enums import MissionStatus, UserRole
from app.schemas.evidence import Evidence
from app.schemas.mission import MissionPlan
from app.schemas.tools import ToolResult


class AuditEvent(BaseModel):
    """An immutable audit trail entry recorded by the Context Service."""

    event_id: NonBlankStr
    mission_id: NonBlankStr
    task_id: Optional[str] = None
    event_type: NonBlankStr
    actor: NonBlankStr
    message: NonBlankStr
    metadata: Dict[str, JsonValue] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class ErrorRecord(BaseModel):
    """A structured error captured from any component, for mission-level diagnosis."""

    error_id: NonBlankStr
    mission_id: NonBlankStr
    task_id: Optional[str] = None
    component: NonBlankStr
    error_type: NonBlankStr
    message: NonBlankStr
    retryable: bool = False
    timestamp: datetime = Field(default_factory=utc_now)


class CampusState(BaseModel):
    """The single shared source of truth for one mission's lifecycle.

    Most fields beyond the identity/goal fields are legitimately absent early
    in a mission's life (no plan yet, no agent results yet, etc.), so they
    default to empty/None rather than being required.
    """

    mission_id: NonBlankStr
    user_id: NonBlankStr
    user_role: UserRole
    original_goal: NonBlankStr
    normalized_goal: Optional[str] = None
    mission_status: MissionStatus = MissionStatus.PENDING
    plan: Optional[MissionPlan] = None
    agent_results: List[AgentResult] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    tool_results: List[ToolResult] = Field(default_factory=list)
    pending_approvals: List[ApprovalRequest] = Field(default_factory=list)
    errors: List[ErrorRecord] = Field(default_factory=list)
    audit_events: List[AuditEvent] = Field(default_factory=list)
    final_result: Optional[str] = None
