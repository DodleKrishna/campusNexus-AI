"""GET /approvals/pending, GET /approvals/stale (Phase 11), POST /approvals/{id}/decision -- the Action Center's backend (§9)."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import Identity, get_identity, get_session, require_role
from app.api.schemas.approvals import ApprovalDecisionRequest, ApprovalView
from app.db.models.mission import ApprovalRecord
from app.db.repositories.missions import list_approvals_by_status, list_approvals_for_step, list_pending_approvals
from app.schemas.enums import ApprovalStatus, UserRole
from app.schemas.evidence import Evidence
from app.services.approval_gate import ApprovalGate, ApprovalGateError
from app.services.context import ContextService

router = APIRouter(tags=["approvals"])


STALE_MESSAGE = (
    "Approval expired because execution conditions changed. Review the updated action and approve again."
)
_REPLACEABLE = (ApprovalStatus.STALE, ApprovalStatus.EDIT_REQUIRED)


def _lifecycle_fields(session: Session, approval: ApprovalRecord, view: ApprovalView) -> None:
    """Phase 11: binding/invalidation fields plus the old-vs-new request links."""
    view.payload_fingerprint = approval.payload_fingerprint
    view.invalidated_at = approval.invalidated_at
    view.invalidation_reason = approval.invalidation_reason
    view.invalidation_details = approval.invalidation_details
    chain = list_approvals_for_step(session, approval.step_id)
    index = next(i for i, a in enumerate(chain) if a.approval_id == approval.approval_id)
    if index > 0 and chain[index - 1].status in _REPLACEABLE:
        view.replaces_approval_id = chain[index - 1].approval_id
    if approval.status in _REPLACEABLE and index + 1 < len(chain):
        view.replaced_by_approval_id = chain[index + 1].approval_id
    if approval.status == ApprovalStatus.STALE:
        view.status_message = STALE_MESSAGE
    elif approval.status == ApprovalStatus.PENDING and view.replaces_approval_id:
        view.status_message = (
            f"New approval request replacing expired request {view.replaces_approval_id}; "
            "the action was re-verified against current data."
        )


def _enrich(session: Session, approval: ApprovalRecord) -> ApprovalView:
    view = ApprovalView(
        approval_id=approval.approval_id, mission_id=approval.mission_id, step_id=approval.step_id,
        status=approval.status.value, requested_by=approval.requested_by, action_summary=approval.action_summary,
        created_at=approval.created_at, decision_by=approval.decision_by, decision_at=approval.decision_at,
        decision_reason=approval.decision_reason,
    )
    _lifecycle_fields(session, approval, view)
    context = ContextService(session)
    runs = [r for r in context.list_agent_runs(approval.mission_id) if r.step_id == approval.step_id]
    # The run that proposed *this* approval (Phase 11 runs record approval_id --
    # a step with a replacement has several proposing runs); for older runs,
    # fall back to the latest run carrying a proposal, then the latest run.
    proposing_run = next(
        (r for r in reversed(runs) if (r.facts or {}).get("approval_id") == approval.approval_id),
        None,
    ) or next((r for r in reversed(runs) if "proposal" in (r.facts or {})), runs[-1] if runs else None)
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
        view.precheck_status = facts.get("precheck_status")
        view.schedule_check = (proposal.get("supporting_facts") or {}).get("schedule_check")
    return view


@router.get("/approvals/pending", response_model=List[ApprovalView])
def list_pending(identity: Identity = Depends(get_identity), session: Session = Depends(get_session)) -> List[ApprovalView]:
    """ADMIN/FACULTY see every pending approval; a STUDENT sees only their own."""
    student_filter: Optional[str] = identity.student_id if identity.role == UserRole.STUDENT else None
    approvals = list_pending_approvals(session, student_id=student_filter)
    return [_enrich(session, a) for a in approvals]


@router.get("/approvals/stale", response_model=List[ApprovalView])
def list_stale(identity: Identity = Depends(get_identity), session: Session = Depends(get_session)) -> List[ApprovalView]:
    """Approvals invalidated because execution conditions changed after a human
    approved them (Phase 11). Same scoping as /approvals/pending."""
    student_filter: Optional[str] = identity.student_id if identity.role == UserRole.STUDENT else None
    approvals = list_approvals_by_status(session, ApprovalStatus.STALE, student_id=student_filter)
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
        if record.status == ApprovalStatus.STALE:
            # Never reactivated: the replacement request is what needs a decision.
            raise HTTPException(status_code=409, detail=f"{STALE_MESSAGE} ({exc})") from exc
        # Already resolved -- a repeated click/submission, not a new decision.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    session.expire_all()
    record = session.get(ApprovalRecord, approval_id)
    return _enrich(session, record)
