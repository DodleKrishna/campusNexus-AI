"""GET /admin/cases -- the Campus Operations view (§10). ADMIN/FACULTY only."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_session, require_role
from app.api.schemas.admin import CaseView
from app.db.repositories.cases import get_case_sla, list_all_cases
from app.rules.sla import compute_sla_breaches
from app.schemas.enums import UserRole

router = APIRouter(tags=["admin"])


@router.get("/admin/cases", response_model=List[CaseView])
def list_cases(_=Depends(require_role(UserRole.ADMIN, UserRole.FACULTY)), session: Session = Depends(get_session)) -> List[CaseView]:
    now = datetime.now(timezone.utc)
    views: List[CaseView] = []
    for case in list_all_cases(session):
        sla = get_case_sla(session, case.case_code)
        response_breached = resolution_breached = None
        response_due = resolution_due = None
        if sla is not None:
            response_due, resolution_due = sla.response_due_at, sla.resolution_due_at
            response_breached, resolution_breached = compute_sla_breaches(
                response_due_at=sla.response_due_at, resolution_due_at=sla.resolution_due_at,
                responded_at=sla.responded_at, resolved_at=sla.resolved_at, now=now,
            )
        views.append(
            CaseView(
                case_code=case.case_code, student_code=case.student.student_code, category=case.category,
                description=case.description, priority=case.priority.value, department=case.department,
                status=case.status.value, created_at=case.created_at,
                response_due_at=response_due, resolution_due_at=resolution_due,
                response_breached=response_breached, resolution_breached=resolution_breached,
            )
        )
    return views
