"""Response/request models for the Action Center / Approval Gate endpoints (§9)."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel

from app.schemas.common import JsonValue
from app.schemas.evidence import Evidence


class ApprovalView(BaseModel):
    approval_id: str
    mission_id: str
    step_id: str
    status: str
    requested_by: str
    action_summary: str
    created_at: datetime
    decision_by: Optional[str] = None
    decision_at: Optional[datetime] = None
    decision_reason: Optional[str] = None

    # Enriched from the proposing AgentRun's persisted facts/evidence
    # (never fabricated -- absent if that data isn't available).
    tool_name: Optional[str] = None
    target_resource: Optional[str] = None
    student_id: Optional[str] = None
    parameters: Dict[str, JsonValue] = {}
    verification_status: Optional[str] = None
    evidence: List[Evidence] = []
    # Issues the propose-time Deterministic Verifier pre-check raised (empty =
    # every pre-check passed) -- what the approver needs to know before deciding.
    precheck_issues: List[str] = []
    # Phase 10: the pre-check's own verdict (verified/needs_review) -- distinct
    # from verification_status, which is "partial" only because the task is
    # paused for approval. None for proposals persisted before Phase 10.
    precheck_status: Optional[str] = None
    # Where the pre-approval schedule-conflict check got its data (register_event only).
    schedule_check: Optional[Dict[str, JsonValue]] = None


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: Optional[str] = None
