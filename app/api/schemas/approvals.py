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

    # Phase 11: payload binding + invalidation. A STALE approval was approved
    # by a human (decision_by/decision_at still say who and when) but the
    # conditions it was granted under stopped holding before execution -- it
    # is NOT a rejection, and it can never authorize anything again.
    payload_fingerprint: Optional[str] = None
    invalidated_at: Optional[datetime] = None
    invalidation_reason: Optional[str] = None
    invalidation_details: Optional[Dict[str, JsonValue]] = None
    # The approval this one replaced (an earlier STALE/EDIT_REQUIRED request for
    # the same step), and the newer request that replaced this one, if any.
    replaces_approval_id: Optional[str] = None
    replaced_by_approval_id: Optional[str] = None
    # One plain-language line explaining the status to an approver.
    status_message: Optional[str] = None
    # Phase 12B/13: where the action's target came from (user_goal,
    # user_selection, caller_supplied, not_required) and a concise label.
    target_source: Optional[str] = None
    target_source_label: Optional[str] = None


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: Optional[str] = None
