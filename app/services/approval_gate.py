"""The Approval Gate (CLAUDE.md component 9).

Enforces human-in-the-loop approval for every sensitive tool call the Action
Agent proposes. Composes the existing ``ContextService`` (never
reimplements its persistence) -- this module owns only the approval
*lifecycle* rule: a pending approval never silently resolves itself, a
rejected action's mission step flips to FAILED (so the scheduler's existing
SKIPPED-cascade guarantees it is never re-dispatched), and an approved
action's step flips back to PENDING so the next
``MissionOrchestrator.resume_mission`` call re-dispatches it -- the
"separate explicit operation" the Phase 7 spec asks for. No LLM calls, no
planning: a deterministic accessor, like the Context Service it wraps.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models.mission import ApprovalRecord
from app.schemas.enums import ApprovalStatus, TaskStatus
from app.services.context import ContextService

_DECIDABLE = {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}


class ApprovalGateError(Exception):
    """An invalid approval operation: unknown id, or already resolved."""


class ApprovalGate:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._context = ContextService(session)

    def request_approval(
        self,
        *,
        mission_id: str,
        step_id: str,
        tool_call_id: str,
        action_summary: str,
        requested_by: str,
    ) -> ApprovalRecord:
        approval_id = f"appr-{uuid.uuid4().hex[:12]}"
        record = self._context.create_approval_record(
            approval_id=approval_id,
            mission_id=mission_id,
            step_id=step_id,
            action_summary=action_summary,
            requested_by=requested_by,
            tool_call_id=tool_call_id,
        )
        self._context.append_audit_event(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            mission_id=mission_id,
            step_id=step_id,
            event_type="approval_requested",
            actor=requested_by,
            message=action_summary,
        )
        return record

    def decide(
        self,
        approval_id: str,
        *,
        decision: ApprovalStatus,
        decision_by: str,
        decision_reason: Optional[str] = None,
    ) -> ApprovalRecord:
        """Resolve a PENDING approval. A pending action never executes automatically,
        and a resolved approval can never be silently re-decided."""
        if decision not in _DECIDABLE:
            raise ApprovalGateError(f"decide() only accepts APPROVED/REJECTED, got {decision!r}")

        record = self._session.get(ApprovalRecord, approval_id)
        if record is None:
            raise ApprovalGateError(f"unknown approval_id: {approval_id!r}")
        if record.status != ApprovalStatus.PENDING:
            raise ApprovalGateError(
                f"approval {approval_id!r} is already resolved (status={record.status.value}); "
                "a resolved approval can never be silently changed"
            )

        now = utc_now()
        updated = self._context.update_approval_record(
            approval_id, decision, decision_by=decision_by, decision_at=now, decision_reason=decision_reason
        )

        if decision == ApprovalStatus.APPROVED:
            # Ready to be re-dispatched by the next resume_mission() call.
            self._context.update_mission_step(record.step_id, status=TaskStatus.PENDING)
        else:
            # REJECTED: terminal. The scheduler never re-dispatches a FAILED step.
            self._context.update_mission_step(record.step_id, status=TaskStatus.FAILED, completed_at=now)

        self._context.append_audit_event(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            mission_id=record.mission_id,
            step_id=record.step_id,
            event_type=f"approval_{decision.value}",
            actor=decision_by,
            message=decision_reason or f"Approval {decision.value} by {decision_by}.",
        )
        return updated

    def mark_superseded(self, approval_id: str, *, requested_by: str, reason: Optional[str] = None) -> ApprovalRecord:
        """Mark a PENDING/APPROVED approval EDIT_REQUIRED because its payload is being
        edited. Never mutates the approved arguments in place -- the caller
        (ActionAgent.propose_edit) creates a *new* proposal/approval pair afterward."""
        record = self._session.get(ApprovalRecord, approval_id)
        if record is None:
            raise ApprovalGateError(f"unknown approval_id: {approval_id!r}")
        if record.status not in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
            raise ApprovalGateError(
                f"approval {approval_id!r} cannot be edited from status={record.status.value}"
            )
        now = utc_now()
        updated = self._context.update_approval_record(
            approval_id,
            ApprovalStatus.EDIT_REQUIRED,
            decision_by=requested_by,
            decision_at=now,
            decision_reason=reason or "Action parameters edited; a fresh approval is required.",
        )
        self._context.append_audit_event(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            mission_id=record.mission_id,
            step_id=record.step_id,
            event_type="approval_edit_requested",
            actor=requested_by,
            message=reason or "Action parameters edited; a fresh approval is required.",
        )
        return updated
