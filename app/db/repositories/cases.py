"""Read-only campus services (case) repository functions."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.identity import Student
from app.db.models.services import CampusCase, CaseSLA


def get_student_cases(session: Session, student_code: str) -> list[CampusCase]:
    stmt = (
        select(CampusCase)
        .join(Student, CampusCase.student_id == Student.id)
        .where(Student.student_code == student_code)
    )
    return list(session.execute(stmt).scalars().all())


def get_case(session: Session, case_code: str) -> CampusCase | None:
    stmt = select(CampusCase).where(CampusCase.case_code == case_code)
    return session.execute(stmt).scalar_one_or_none()


def get_case_sla(session: Session, case_code: str) -> CaseSLA | None:
    stmt = (
        select(CaseSLA)
        .join(CampusCase, CaseSLA.case_id == CampusCase.id)
        .where(CampusCase.case_code == case_code)
    )
    return session.execute(stmt).scalar_one_or_none()


def list_all_cases(session: Session) -> list[CampusCase]:
    """Every campus case, across every student -- the Phase 8 Campus
    Operations admin view. Student-scoped reads still go through
    ``get_student_cases``; this is deliberately unscoped and therefore only
    ever exposed behind an ADMIN/FACULTY role check at the API boundary."""
    stmt = select(CampusCase).order_by(CampusCase.created_at.desc())
    return list(session.execute(stmt).scalars().all())
