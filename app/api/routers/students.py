"""GET /demo/students, GET /students/{id}/dashboard, GET /students/{id}/calendar."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.dashboard import build_dashboard
from app.api.deps import Identity, get_identity, get_knowledge_service, get_session, require_student_ownership
from app.api.identities import DEMO_IDENTITIES
from app.api.schemas.common import DemoIdentityView
from app.api.schemas.students import CalendarEntryView, DashboardResponse
from app.db.repositories.calendar import get_student_calendar
from app.db.repositories.students import get_student_by_id
from app.schemas.enums import UserRole
from app.services.knowledge import KnowledgeService

router = APIRouter(tags=["students"])


@router.get("/demo/students", response_model=List[DemoIdentityView])
def list_demo_students(session: Session = Depends(get_session)) -> List[DemoIdentityView]:
    """The available STUDENT demo identities -- a pre-auth discovery endpoint
    (there is no identity yet to check at this point in the flow)."""
    views: List[DemoIdentityView] = []
    for definition in DEMO_IDENTITIES.values():
        if definition.role != UserRole.STUDENT:
            continue
        student = get_student_by_id(session, definition.student_id)
        display_name = f"{student.user.full_name} (Student)" if student is not None else definition.fallback_display_name
        views.append(DemoIdentityView(key=definition.key, role=definition.role.value, display_name=display_name, student_id=definition.student_id))
    return views


@router.get("/students/{student_id}/dashboard", response_model=DashboardResponse)
def get_dashboard(
    student_id: str,
    identity: Identity = Depends(get_identity),
    session: Session = Depends(get_session),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> DashboardResponse:
    require_student_ownership(identity, student_id)
    if get_student_by_id(session, student_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown student_id {student_id!r}")
    return build_dashboard(session, knowledge_service, student_id)


@router.get("/students/{student_id}/calendar", response_model=List[CalendarEntryView])
def get_calendar(
    student_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(get_session)
) -> List[CalendarEntryView]:
    require_student_ownership(identity, student_id)
    if get_student_by_id(session, student_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown student_id {student_id!r}")
    return [
        CalendarEntryView(id=e.id, title=e.title, start_at=e.start_at, end_at=e.end_at, source_type=e.source_type, source_id=e.source_id)
        for e in get_student_calendar(session, student_id)
    ]
