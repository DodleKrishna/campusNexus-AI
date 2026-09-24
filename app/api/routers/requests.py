"""/requests/* (Phase 16): student workflow requests (permission, leave, OD).

    student:  POST /requests/prepare  -> Permission Agent prepares a DRAFT (preview)
              POST /requests          -> Confirm & Send that exact draft (PENDING)
              POST /requests/{id}/cancel
    faculty:  POST /requests/{id}/approve | /reject  (only the routed reviewer)
    both:     GET /requests, GET /requests/{id}  (own requests / requests routed to me)

Separate from ``/approvals`` (Action Agent tool-call approvals). Nothing here
calls the Action Agent, the Tool Gateway or the Approval Gate.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.agents.permission.agent import PermissionAgent
from app.api.auth_deps import (
    AuthenticatedUser,
    FacultyCaller,
    current_faculty_profile,
    current_student_account,
    require_authenticated_user,
)
from app.api.deps import get_knowledge_service, get_now, get_session
from app.db.models.auth import AuthAccount
from app.llm.base import LLMProviderError, LLMTransientError
from app.schemas.enums import UserRole
from app.schemas.workflow import (
    DecisionBody,
    PermissionPreview,
    PrepareRequestBody,
    SubmitRequestBody,
    WorkflowRequestView,
)
from app.services import workflow_requests
from app.services.faculty_ops import faculty_for_account
from app.services.knowledge import KnowledgeService
from app.services.workflow_requests import RequestError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/requests", tags=["workflow requests"])


def _run(operation):
    try:
        return operation()
    except RequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("", response_model=List[WorkflowRequestView])
def list_requests(user: AuthenticatedUser = Depends(require_authenticated_user), session: Session = Depends(get_session)) -> List[WorkflowRequestView]:
    if user.role == UserRole.STUDENT and user.student_id:
        rows = workflow_requests.list_for_student(session, user.student_id)
    elif user.role in (UserRole.FACULTY, UserRole.HOD):
        faculty = faculty_for_account(session, user.account_id)
        if faculty is None:
            raise HTTPException(status_code=403, detail="This account is not linked to a faculty profile.")
        rows = workflow_requests.list_for_reviewer(session, faculty)
    else:
        raise HTTPException(status_code=403, detail="Your role does not have access to this.")
    return [workflow_requests.view(session, r) for r in rows]


@router.get("/{request_id}", response_model=WorkflowRequestView)
def get_request(
    request_id: str, user: AuthenticatedUser = Depends(require_authenticated_user), session: Session = Depends(get_session),
) -> WorkflowRequestView:
    request = workflow_requests.get_request(session, request_id)
    allowed = False
    if request is not None and user.role == UserRole.STUDENT:
        allowed = request.student_id == user.student_id
    elif request is not None and user.role in (UserRole.FACULTY, UserRole.HOD):
        faculty = faculty_for_account(session, user.account_id)
        allowed = faculty is not None and request.reviewer_faculty_id == faculty.id and request.submitted_at is not None
    if not allowed:
        raise HTTPException(status_code=404, detail="That request was not found.")
    return workflow_requests.view(session, request)


@router.post("/prepare", response_model=PermissionPreview)
def prepare(
    body: PrepareRequestBody, request: Request, account: AuthAccount = Depends(current_student_account),
    session: Session = Depends(get_session), knowledge: KnowledgeService = Depends(get_knowledge_service),
    now: datetime = Depends(get_now),
) -> PermissionPreview:
    student = workflow_requests.student_record(session, account.linked_student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Your student record was not found.")
    agent = PermissionAgent(llm_provider=request.app.state.llm_provider)
    try:
        return agent.prepare(
            session, account=account, student=student, message=body.message, now=now,
            required_percentage=workflow_requests.threshold(knowledge, now),
        )
    except LLMTransientError as exc:
        logger.warning("permission agent: provider unavailable (%s)", exc.details())
        raise HTTPException(status_code=503, detail="Live AI is temporarily unavailable. Please try again shortly.") from exc
    except LLMProviderError as exc:
        logger.error("permission agent: provider error: %s", exc)
        raise HTTPException(status_code=502, detail="The AI provider returned an unusable response. Please try again.") from exc


@router.post("", response_model=WorkflowRequestView)
def submit(
    body: SubmitRequestBody, account: AuthAccount = Depends(current_student_account),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> WorkflowRequestView:
    request = _run(lambda: workflow_requests.submit(session, account, account.linked_student_id, body.request_id, body.reason, now))
    return workflow_requests.view(session, request)


@router.post("/{request_id}/cancel", response_model=WorkflowRequestView)
def cancel(
    request_id: str, account: AuthAccount = Depends(current_student_account),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> WorkflowRequestView:
    request = _run(lambda: workflow_requests.cancel(session, account, account.linked_student_id, request_id, now))
    return workflow_requests.view(session, request)


def _decide(request_id: str, approve: bool, body: DecisionBody, caller: FacultyCaller, session: Session, now: datetime) -> WorkflowRequestView:
    request = _run(lambda: workflow_requests.decide(session, caller.account, caller.faculty, request_id, approve, body.comment, now))
    return workflow_requests.view(session, request)


@router.post("/{request_id}/approve", response_model=WorkflowRequestView)
def approve(
    request_id: str, body: DecisionBody, caller: FacultyCaller = Depends(current_faculty_profile),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> WorkflowRequestView:
    return _decide(request_id, True, body, caller, session, now)


@router.post("/{request_id}/reject", response_model=WorkflowRequestView)
def reject(
    request_id: str, body: DecisionBody, caller: FacultyCaller = Depends(current_faculty_profile),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> WorkflowRequestView:
    return _decide(request_id, False, body, caller, session, now)
