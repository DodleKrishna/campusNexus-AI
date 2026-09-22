"""Read-only career repository functions."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.career import Application, Opportunity, OpportunityStatus, StudentSkill
from app.db.models.identity import Student


def get_student_skills(session: Session, student_code: str) -> list[StudentSkill]:
    stmt = (
        select(StudentSkill)
        .join(Student, StudentSkill.student_id == Student.id)
        .where(Student.student_code == student_code)
    )
    return list(session.execute(stmt).scalars().all())


def list_active_opportunities(session: Session) -> list[Opportunity]:
    """Opportunities currently marked OPEN. Expired/closed filtering by deadline is a
    deterministic rule concern (app/rules/), not this repository's job."""
    stmt = select(Opportunity).where(Opportunity.status == OpportunityStatus.OPEN)
    return list(session.execute(stmt).scalars().all())


def get_opportunity_requirements(session: Session, opportunity_id: int) -> Opportunity | None:
    """An opportunity with its eligibility relations loaded (departments/years/skills)."""
    stmt = select(Opportunity).where(Opportunity.id == opportunity_id)
    return session.execute(stmt).scalar_one_or_none()


def get_applications(session: Session, student_code: str) -> list[Application]:
    stmt = (
        select(Application)
        .join(Student, Application.student_id == Student.id)
        .where(Student.student_code == student_code)
    )
    return list(session.execute(stmt).scalars().all())
