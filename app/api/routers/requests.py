"""/requests/* (Phase 16 students, Phase 17 faculty/HOD): permission, leave, OD and faculty requests.

    requester (student or faculty):
              POST /requests/prepare  -> Permission Agent prepares a DRAFT (preview)
              POST /requests          -> Confirm & Send that exact draft (PENDING)
              POST /requests/{id}/cancel
    reviewer (faculty or HOD):
              POST /requests/{id}/approve | /reject  (only the routed reviewer, never one's own)
    GET /requests?box=inbox|mine    students always get their own; faculty/HOD choose
    GET /requests/{id}

Separate from ``/approvals`` (Action Agent tool-call approvals). Nothing here
calls the Action Agent, the Tool Gateway or the Approval Gate. Identity,
department and faculty profile come from the token's account only.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.agents.permission.agent import PermissionAgent
from app.api.auth_deps import AuthenticatedUser, current_faculty_profile, require_authenticated_user
from app.api.deps import get_knowledge_service, get_now, get_session
from app.auth.accounts import get_account
from app.llm.base import LLMProviderError, LLMTransientError
from app.schemas.enums import UserRole
from app.schemas.workflow import (
    DecisionBody,
    PermissionPreview,
    PrepareRequestBody,
    SubmitRequestBody,
    WorkflowRequestView,
)
from app.services import department_ops, workflow_requests
from app.db.models.faculty import FacultyProfile
from app.services.faculty_ops import faculty_for_account
from app.services.knowledge import KnowledgeService
from app.services.workflow_requests import RequestError, Requester

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/requests", tags=["workflow requests"])
_STAFF = (UserRole.FACULTY, UserRole.HOD)


def _run(operation):
    try:
        return operation()
    except RequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _requester(session: Session, user: AuthenticatedUser) -> Requester:
    """Students and faculty/HOD may request; the requester is resolved from the account link only."""
    account = get_account(session, user.account_id)
    if account is None:
        raise HTTPException(status_code=401, detail="Invalid session. Please sign in again.")
    # Phase 22B: the student / faculty profile and the role come from the organization membership.
    if user.role == UserRole.STUDENT and user.student_id:
        student = workflow_requests.student_record(session, user.student_id)
        if student is not None:
            return Requester(account=account, student=student, role=user.role)
    if user.role in _STAFF and user.faculty_profile_id:
        faculty = session.get(FacultyProfile, user.faculty_profile_id)
        if faculty is not None:
            return Requester(account=account, faculty=faculty, role=user.role)
    raise HTTPException(status_code=403, detail="Your account cannot make permission requests.")


@router.get("", response_model=List[WorkflowRequestView])
def list_requests(
    box: Literal["inbox", "mine"] = Query(default="inbox"),
    user: AuthenticatedUser = Depends(require_authenticated_user), session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> List[WorkflowRequestView]:
    if user.role == UserRole.STUDENT and user.student_id:
        rows = workflow_requests.list_for_student(session, user.student_id)
    elif user.role in _STAFF:
        faculty = faculty_for_account(session, user.account_id)
        if faculty is None:
            raise HTTPException(status_code=403, detail="This account is not linked to a faculty profile.")
        if box == "mine":
            rows = workflow_requests.list_own_faculty(session, faculty)
        else:
            scope = department_ops.hod_scope(session, user.account_id, user.faculty_profile_id)
            if scope is not None:
                department_ops.escalate_unassigned(session, scope, now)
            rows = workflow_requests.list_for_reviewer(session, faculty)
    elif user.role == UserRole.ADMIN:
        # Phase 18: the administration's inbox. Anything stuck with no reviewer is escalated here first.
        workflow_requests.escalate_unassigned_to_admin(session, now)
        rows = workflow_requests.list_for_admin(session)
    else:
        raise HTTPException(status_code=403, detail="Your role does not have access to this.")
    return [workflow_requests.view(session, r) for r in rows]


@router.get("/{request_id}", response_model=WorkflowRequestView)
def get_request(
    request_id: str, user: AuthenticatedUser = Depends(require_authenticated_user), session: Session = Depends(get_session),
) -> WorkflowRequestView:
    request = workflow_requests.get_request(session, request_id)
    allowed = request is not None and request.requester_account_id == user.account_id and (
        user.role != UserRole.STUDENT or request.submitted_at is not None
    )
    if request is not None and not allowed and user.role == UserRole.ADMIN:
        allowed = request.submitted_at is not None  # institution-wide visibility for administrators
    if request is not None and not allowed and user.role in _STAFF:
        faculty = faculty_for_account(session, user.account_id)
        allowed = faculty is not None and request.reviewer_faculty_id == faculty.id and request.submitted_at is not None
    if not allowed:
        raise HTTPException(status_code=404, detail="That request was not found.")
    return workflow_requests.view(session, request)


@router.post("/prepare", response_model=PermissionPreview)
def prepare(
    body: PrepareRequestBody, request: Request, user: AuthenticatedUser = Depends(require_authenticated_user),
    session: Session = Depends(get_session), knowledge: KnowledgeService = Depends(get_knowledge_service),
    now: datetime = Depends(get_now),
) -> PermissionPreview:
    requester = _requester(session, user)
    agent = PermissionAgent(llm_provider=request.app.state.llm_provider)
    try:
        return agent.prepare(
            session, requester=requester, message=body.message, now=now,
            required_percentage=workflow_requests.threshold(knowledge, now) if requester.kind == "student" else None,
        )
    except LLMTransientError as exc:
        logger.warning("permission agent: provider unavailable (%s)", exc.details())
        raise HTTPException(status_code=503, detail="Live AI is temporarily unavailable. Please try again shortly.") from exc
    except LLMProviderError as exc:
        logger.error("permission agent: provider error: %s", exc)
        raise HTTPException(status_code=502, detail="The AI provider returned an unusable response. Please try again.") from exc


@router.post("", response_model=WorkflowRequestView)
def submit(
    body: SubmitRequestBody, user: AuthenticatedUser = Depends(require_authenticated_user),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> WorkflowRequestView:
    requester = _requester(session, user)
    request = _run(lambda: workflow_requests.submit(session, requester.account, body.request_id, body.reason, now))
    return workflow_requests.view(session, request)


@router.post("/{request_id}/cancel", response_model=WorkflowRequestView)
def cancel(
    request_id: str, user: AuthenticatedUser = Depends(require_authenticated_user),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> WorkflowRequestView:
    requester = _requester(session, user)
    request = _run(lambda: workflow_requests.cancel(session, requester.account, request_id, now))
    return workflow_requests.view(session, request)


def _decide(request_id: str, approve: bool, body: DecisionBody, user: AuthenticatedUser, session: Session, now: datetime) -> WorkflowRequestView:
    """Faculty/HOD decide requests routed to them; administrators decide requests routed to the administration."""
    if user.role == UserRole.ADMIN:
        account = get_account(session, user.account_id)
        request = _run(lambda: workflow_requests.decide_as_admin(session, account, request_id, approve, body.comment, now))
        return workflow_requests.view(session, request)
    if user.role not in _STAFF:
        raise HTTPException(status_code=403, detail="Your role does not have access to this.")
    caller = current_faculty_profile(user, session)
    request = _run(lambda: workflow_requests.decide(session, caller.account, caller.faculty, request_id, approve, body.comment, now))
    return workflow_requests.view(session, request)


@router.post("/{request_id}/approve", response_model=WorkflowRequestView)
def approve(
    request_id: str, body: DecisionBody, user: AuthenticatedUser = Depends(require_authenticated_user),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> WorkflowRequestView:
    return _decide(request_id, True, body, user, session, now)


@router.post("/{request_id}/reject", response_model=WorkflowRequestView)
def reject(
    request_id: str, body: DecisionBody, user: AuthenticatedUser = Depends(require_authenticated_user),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> WorkflowRequestView:
    return _decide(request_id, False, body, user, session, now)
