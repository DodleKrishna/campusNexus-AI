"""GET /approvals/pending, POST /approvals/{id}/decision -- the Action Center's backend (§9)."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import Identity, get_identity, get_session, require_role
from app.api.schemas.approvals import ApprovalDecisionRequest, ApprovalView
from app.db.models.mission import ApprovalRecord
from app.db.repositories.missions import list_pending_approvals
from app.schemas.enums import ApprovalStatus, UserRole
from app.schemas.evidence import Evidence
from app.services.approval_gate import ApprovalGate, ApprovalGateError
from app.services.context import ContextService

router = APIRouter(tags=["approvals"])


def _enrich(session: Session, approval: ApprovalRecord) -> ApprovalView:
    view = ApprovalView(
        approval_id=approval.approval_id, mission_id=approval.mission_id, step_id=approval.step_id,
        status=approval.status.value, requested_by=approval.requested_by, action_summary=approval.action_summary,
        created_at=approval.created_at, decision_by=approval.decision_by, decision_at=approval.decision_at,
        decision_reason=approval.decision_reason,
    )
    context = ContextService(session)
    runs = [r for r in context.list_agent_runs(approval.mission_id) if r.step_id == approval.step_id]
    # Prefer the run that actually carries the proposal payload (the first,
    # propose-time dispatch); fall back to whatever's most recent.
    proposing_run = next((r for r in reversed(runs) if "proposal" in (r.facts or {})), runs[-1] if runs else None)
    if proposing_run is not None:
        facts = proposing_run.facts or {}
        proposal = facts.get("proposal") or {}
        view.tool_name = facts.get("tool_name")
        view.target_resource = proposal.get("target_resource")
        view.student_id = proposal.get("student_id")
        view.parameters = proposal.get("parameters") or {}
        view.verification_status = proposing_run.status.value
        view.evidence = [Evidence.model_validate(e) for e in (proposing_run.evidence or [])]
        view.precheck_issues = list(proposing_run.errors or [])
    return view


@router.get("/approvals/pending", response_model=List[ApprovalView])
def list_pending(identity: Identity = Depends(get_identity), session: Session = Depends(get_session)) -> List[ApprovalView]:
    """ADMIN/FACULTY see every pending approval; a STUDENT sees only their own."""
    student_filter: Optional[str] = identity.student_id if identity.role == UserRole.STUDENT else None
    approvals = list_pending_approvals(session, student_id=student_filter)
    return [_enrich(session, a) for a in approvals]


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalView)
def decide(
    approval_id: str,
    body: ApprovalDecisionRequest,
    identity: Identity = Depends(require_role(UserRole.ADMIN)),
    session: Session = Depends(get_session),
) -> ApprovalView:
    record = session.get(ApprovalRecord, approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown approval_id {approval_id!r}")

    decision = ApprovalStatus.APPROVED if body.decision == "approve" else ApprovalStatus.REJECTED
    gate = ApprovalGate(session)
    try:
        gate.decide(approval_id, decision=decision, decision_by=identity.display_name, decision_reason=body.reason)
    except ApprovalGateError as exc:
        # Already resolved -- a repeated click/submission, not a new decision.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    session.expire_all()
    record = session.get(ApprovalRecord, approval_id)
    return _enrich(session, record)
