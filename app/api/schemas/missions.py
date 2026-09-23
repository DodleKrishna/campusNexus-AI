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


class ApprovalSummaryView(BaseModel):
    approval_id: str
    step_id: str
    action_summary: str
    status: str


class MissionResponse(BaseModel):
    mission_id: str
    goal: str
    status: str
    plan: List[MissionTaskView]
    agent_results: List[AgentRunView]
    pending_approvals: List[ApprovalSummaryView] = []
    final_result: Optional[str] = None
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
