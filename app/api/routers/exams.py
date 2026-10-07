"""/exams (JWT, AgentOS V2 Phase 4): managed exam lifecycle, exam attendance and the student's own exams.

Organization, account, role and student/faculty profile come only from the verified token; bodies forbid extra
fields, so no client can name an organization, a creator, a marker or a student identity. An exam the caller may
not see is a 404 (no existence oracle). Scheduling creates the Exam Guardian mission; nothing here runs the
Guardian -- the due-mission worker does (``scripts/process_due_missions.py``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, List, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.agentos.runtime import AgentOSError, MissionActor
from app.api.auth_deps import AuthenticatedUser, require_roles
from app.api.deps import get_now, get_session
from app.rules.exam_rules import ExamPolicy
from app.schemas.enums import UserRole
from app.services import exams as service
from app.services.exams import (
    CreateExam, ExamError, ExamProgressView, ExamView, MarkExamAttendance, MarkResult, MyExamView, ScheduleResult,
)

router = APIRouter(prefix="/exams", tags=["exams"])
T = TypeVar("T")

_staff = require_roles(UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN)
_student = require_roles(UserRole.STUDENT)
_anyone = require_roles(UserRole.STUDENT, UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN)


def _actor(user: AuthenticatedUser) -> MissionActor:
    return MissionActor(organization_id=user.organization_id, account_id=user.account_id, role=user.role,
                        student_code=user.student_id, faculty_profile_id=user.faculty_profile_id)


def _policy(request: Request) -> ExamPolicy:
    """The policy the registered Exam Guardian enforces (so the API and the Guardian report the same deadline)."""
    spec = request.app.state.agent_runtime.agents.get(service.GUARDIAN_AGENT_KEY)
    return spec.supervisor.policy if spec is not None else ExamPolicy()


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (ExamError, AgentOSError) as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from None


@router.post("", response_model=ExamView, status_code=201)
def create_exam(body: CreateExam, user: AuthenticatedUser = Depends(_staff), session: Session = Depends(get_session),
                now: datetime = Depends(get_now)) -> ExamView:
    return _run(lambda: service.view(session, service.create_exam(session, _actor(user), body, now)))


@router.get("/my", response_model=List[MyExamView])
def my_exams(user: AuthenticatedUser = Depends(_student), session: Session = Depends(get_session)) -> List[MyExamView]:
    return _run(lambda: service.my_exams(session, _actor(user)))


@router.post("/{exam_id}/schedule", response_model=ScheduleResult)
def schedule(exam_id: int, request: Request, user: AuthenticatedUser = Depends(_staff),
             session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> ScheduleResult:
    runtime = request.app.state.agent_runtime
    return _run(lambda: service.schedule(session, runtime, _actor(user), exam_id, now))


@router.post("/{exam_id}/start", response_model=ExamView)
def start(exam_id: int, user: AuthenticatedUser = Depends(_staff), session: Session = Depends(get_session),
          now: datetime = Depends(get_now)) -> ExamView:
    return _run(lambda: service.view(session, service.start(session, _actor(user), exam_id, now)))


@router.post("/{exam_id}/attendance", response_model=MarkResult)
def mark_attendance(exam_id: int, body: MarkExamAttendance, user: AuthenticatedUser = Depends(_staff),
                    session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> MarkResult:
    return _run(lambda: service.mark_attendance(session, _actor(user), exam_id, body, now))


@router.post("/{exam_id}/complete", response_model=ExamView)
def complete(exam_id: int, user: AuthenticatedUser = Depends(_staff), session: Session = Depends(get_session),
             now: datetime = Depends(get_now)) -> ExamView:
    return _run(lambda: service.view(session, service.complete(session, _actor(user), exam_id, now)))


@router.post("/{exam_id}/cancel", response_model=ExamView)
def cancel(exam_id: int, user: AuthenticatedUser = Depends(_staff), session: Session = Depends(get_session),
           now: datetime = Depends(get_now)) -> ExamView:
    return _run(lambda: service.view(session, service.cancel(session, _actor(user), exam_id, now)))


@router.get("/{exam_id}", response_model=ExamView)
def get_exam(exam_id: int, user: AuthenticatedUser = Depends(_anyone), session: Session = Depends(get_session)) -> ExamView:
    """Staff who manage it, or a targeted student (never a draft). The view holds no other student's data."""
    actor = _actor(user)
    if user.role == UserRole.STUDENT:
        return _run(lambda: service.view(session, service.visible_to_student(session, actor, exam_id)))
    return _run(lambda: service.view(session, service.manageable(session, actor, exam_id)))


@router.get("/{exam_id}/progress", response_model=ExamProgressView)
def progress(exam_id: int, request: Request, user: AuthenticatedUser = Depends(_staff),
             session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> ExamProgressView:
    return _run(lambda: service.progress_view(session, service.manageable(session, _actor(user), exam_id), now,
                                              _policy(request)))
