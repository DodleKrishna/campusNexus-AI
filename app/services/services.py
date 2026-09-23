"""The Campus Services Service: read-only tools over the Phase 2 case
repositories (app/db/repositories/cases.py). Mirrors app/services/academic.py.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.models.services import CaseSLA
from app.db.repositories.cases import get_case_sla, get_student_cases
from app.schemas.services import CaseSummary


def _case_summary(case) -> CaseSummary:
    return CaseSummary(
        case_code=case.case_code,
        student_code=case.student.student_code,
        category=case.category,
        description=case.description,
        priority=case.priority.value,
        department=case.department,
        status=case.status.value,
        created_at=case.created_at,
    )


def get_student_case_summaries(session: Session, student_id: str) -> List[CaseSummary]:
    return [_case_summary(c) for c in get_student_cases(session, student_id)]


def get_case_sla_record(session: Session, case_code: str) -> Optional[CaseSLA]:
    """Raw SLA timestamps for one case -- the deterministic rule's input (app/rules/sla.py)."""
    return get_case_sla(session, case_code)
