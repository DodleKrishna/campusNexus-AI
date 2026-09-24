"""Response/request models for the mission endpoints (§2, §6, §7, §8)."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import JsonValue
from app.schemas.evidence import Evidence


class MissionCreateRequest(BaseModel):
    goal: str = Field(min_length=3)
    # Only honored for ADMIN/FACULTY callers (staff assisting a student); a
    # STUDENT caller's own identity always wins -- enforced in the router,
    # never trusted from this field alone.
    student_id: Optional[str] = None


class MissionTaskView(BaseModel):
    task_id: str
    agent: str
    objective: str
    dependencies: List[str] = []
    status: str


class AgentRunView(BaseModel):
    task_id: str
    agent: str
    status: str
    facts: Dict[str, JsonValue] = {}
    errors: List[str] = []
    evidence: List[Evidence] = []
    # Phase 9 (additive): the agent's own user-facing answer for this task,
    # generated from its verified facts -- previously persisted but hidden.
    response_text: str = ""
    # Phase 10 (additive): True when this run repeated the previous run of the
    # same task exactly (same status, errors and answer) -- the UI folds it
    # into that earlier run instead of showing an identical card again.
    identical_to_previous_run: bool = False


class ApprovalSummaryView(BaseModel):
    approval_id: str
    step_id: str
    action_summary: str
    status: str
    invalidation_reason: Optional[str] = None
    replaced_by_approval_id: Optional[str] = None


class ExecutionStopView(BaseModel):
    """Why the Orchestrator stopped replanning (Phase 10), from the audit trail."""

    reason: str  # "duplicate_failure"
    message: str
    task_ids: List[str] = []


class MissionResponse(BaseModel):
    mission_id: str
    goal: str
    status: str
    plan: List[MissionTaskView]
    agent_results: List[AgentRunView]
    pending_approvals: List[ApprovalSummaryView] = []
    # Phase 11: approvals that expired because execution conditions changed.
    stale_approvals: List[ApprovalSummaryView] = []
    final_result: Optional[str] = None
    execution_stop: Optional[ExecutionStopView] = None
    # Post-12C (additive): an action the student asked for is waiting for them
    # to select its target (derived from the latest plan, never from prose).
    user_selection_required: bool = False
    created_at: datetime
    updated_at: datetime


class TimelineEntryView(BaseModel):
    event_id: str
    timestamp: datetime
    status: str  # PLANNING/RUNNING/VERIFYING/WAITING_FOR_APPROVAL/COMPLETED/NEEDS_REVIEW/FAILED
    event_type: str  # the real underlying AuditLog.event_type / a step's started_at marker
    actor: str
    message: str
    step_id: Optional[str] = None


class MissionTimelineResponse(BaseModel):
    mission_id: str
    entries: List[TimelineEntryView]


class TaskEvidenceView(BaseModel):
    task_id: str
    agent: str
    evidence: List[Evidence]


class MissionEvidenceResponse(BaseModel):
    mission_id: str
    tasks: List[TaskEvidenceView]
