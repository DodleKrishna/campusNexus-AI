"""/assignments (JWT, AgentOS V2 Phase 3): faculty assignment lifecycle and student submissions.

Organization, account, role and student/faculty profile come only from the verified token;
bodies forbid extra fields, so no client can name an organization, a creator, a faculty
member or a student. An assignment the caller may not see is a 404 (no existence oracle).
Publishing creates the Assignment Guardian mission; nothing here runs the Guardian -- the
due-mission worker does (``scripts/process_due_missions.py``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, List, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.agentos.runtime import AgentOSError, MissionActor
from app.api.auth_deps import AuthenticatedUser, require_roles
from app.api.deps import get_now, get_session
from app.schemas.enums import UserRole
from app.services import assignments as service
from app.services.assignments import (
    AssignmentError, AssignmentView, CreateAssignment, MyAssignmentView, ProgressView, PublishResult, SubmissionView,
    SubmitAssignment,
)

router = APIRouter(prefix="/assignments", tags=["assignments"])
T = TypeVar("T")

_staff = require_roles(UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN)
_student = require_roles(UserRole.STUDENT)


def _actor(user: AuthenticatedUser) -> MissionActor:
    return MissionActor(organization_id=user.organization_id, account_id=user.account_id, role=user.role,
                        student_code=user.student_id, faculty_profile_id=user.faculty_profile_id)


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (AssignmentError, AgentOSError) as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from None


@router.post("", response_model=AssignmentView, status_code=201)
def create_assignment(body: CreateAssignment, user: AuthenticatedUser = Depends(_staff),
                      session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> AssignmentView:
    return _run(lambda: service.view(session, service.create_assignment(session, _actor(user), body, now)))


@router.get("/my", response_model=List[MyAssignmentView])
def my_assignments(user: AuthenticatedUser = Depends(_student),
                   session: Session = Depends(get_session)) -> List[MyAssignmentView]:
    return _run(lambda: service.my_assignments(session, _actor(user)))


@router.post("/{assignment_id}/publish", response_model=PublishResult)
def publish(assignment_id: int, request: Request, user: AuthenticatedUser = Depends(_staff),
            session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> PublishResult:
    runtime = request.app.state.agent_runtime
    return _run(lambda: service.publish(session, runtime, _actor(user), assignment_id, now))


@router.post("/{assignment_id}/cancel", response_model=AssignmentView)
def cancel(assignment_id: int, user: AuthenticatedUser = Depends(_staff), session: Session = Depends(get_session),
           now: datetime = Depends(get_now)) -> AssignmentView:
    return _run(lambda: service.view(session, service.cancel(session, _actor(user), assignment_id, now)))


@router.get("/{assignment_id}", response_model=AssignmentView)
def get_assignment(assignment_id: int, user: AuthenticatedUser = Depends(_staff),
                   session: Session = Depends(get_session)) -> AssignmentView:
    return _run(lambda: service.view(session, service.manageable(session, _actor(user), assignment_id)))


@router.get("/{assignment_id}/progress", response_model=ProgressView)
def progress(assignment_id: int, user: AuthenticatedUser = Depends(_staff), session: Session = Depends(get_session),
             now: datetime = Depends(get_now)) -> ProgressView:
    return _run(lambda: service.progress_view(session, service.manageable(session, _actor(user), assignment_id), now))


@router.post("/{assignment_id}/submit", response_model=SubmissionView, status_code=201)
def submit(assignment_id: int, body: SubmitAssignment, user: AuthenticatedUser = Depends(_student),
           session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> SubmissionView:
    def operation() -> SubmissionView:
        row = service.submit(session, _actor(user), assignment_id, body, now)
        return SubmissionView(assignment_id=row.assignment_id, status=row.status, submitted_at=row.submitted_at)

    return _run(operation)
