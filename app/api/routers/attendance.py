"""/attendance (JWT, AgentOS V2 Phase 4): staff view of attendance interventions and justification approval.

Attendance marks are still written only by ``/faculty/classes/{id}/attendance`` (Phase 16), which now also
publishes the normalized attendance events. Scope is structural (admin: the organization; faculty: their own
classes; HOD: also their department) and comes only from the token; anything else is a 404.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, List, Optional, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agentos.runtime import MissionActor
from app.api.auth_deps import AuthenticatedUser, require_roles
from app.api.deps import get_now, get_session
from app.db.models.attendance_intervention import InterventionStatus
from app.schemas.enums import UserRole
from app.services import attendance_monitor as service
from app.services.attendance_monitor import AttendanceMonitorError, InterventionView, JustifyAbsence

router = APIRouter(prefix="/attendance", tags=["attendance"])
T = TypeVar("T")

_staff = require_roles(UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN)


def _actor(user: AuthenticatedUser) -> MissionActor:
    return MissionActor(organization_id=user.organization_id, account_id=user.account_id, role=user.role,
                        student_code=user.student_id, faculty_profile_id=user.faculty_profile_id)


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except AttendanceMonitorError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from None


@router.get("/interventions", response_model=List[InterventionView])
def list_interventions(status: Optional[InterventionStatus] = Query(default=None),
                       user: AuthenticatedUser = Depends(_staff),
                       session: Session = Depends(get_session)) -> List[InterventionView]:
    return _run(lambda: [service.view(session, i) for i in service.list_for_staff(session, _actor(user), status=status)])


@router.get("/interventions/{intervention_id}", response_model=InterventionView)
def get_intervention(intervention_id: int, user: AuthenticatedUser = Depends(_staff),
                     session: Session = Depends(get_session)) -> InterventionView:
    return _run(lambda: service.view(session, service.manageable(session, _actor(user), intervention_id)))


@router.post("/interventions/{intervention_id}/justify", response_model=InterventionView)
def justify(intervention_id: int, body: JustifyAbsence, user: AuthenticatedUser = Depends(_staff),
            session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> InterventionView:
    return _run(lambda: service.view(session, service.justify(session, _actor(user), intervention_id, body, now)))
