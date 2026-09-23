"""The Career Service: read-only tools over the Phase 2 career repositories
(app/db/repositories/career.py, built ahead of need in Phase 2). No arbitrary
SQL, no LLM calls, no write access -- mirrors app/services/academic.py.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.repositories.career import (
    get_applications as _get_applications,
    get_student_skills,
    list_active_opportunities,
)
from app.db.repositories.students import get_student_by_id
from app.schemas.career import (
    ApplicationSummary,
    CareerProfile,
    OpportunitySummary,
    RequiredSkillSummary,
    StudentSkillSummary,
)


def get_career_profile(session: Session, student_id: str) -> Optional[CareerProfile]:
    """A typed snapshot of the student's career-relevant identity + skills, or None if unknown."""
    student = get_student_by_id(session, student_id)
    if student is None:
        return None
    skills = get_student_skills(session, student_id)
    return CareerProfile(
        student_code=student.student_code,
        full_name=student.user.full_name,
        department_code=student.department.code,
        year=student.year,
        cgpa=student.cgpa,
        skills=[StudentSkillSummary(name=s.skill.name, proficiency=s.proficiency) for s in skills],
    )


def _opportunity_summary(opportunity) -> OpportunitySummary:
    return OpportunitySummary(
        opportunity_id=opportunity.id,
        title=opportunity.title,
        company=opportunity.company.name,
        opportunity_type=opportunity.opportunity_type.value,
        minimum_cgpa=opportunity.minimum_cgpa,
        deadline=opportunity.deadline,
        status=opportunity.status.value,
        allowed_departments=[d.department.code for d in opportunity.allowed_departments],
        eligible_years=[y.year for y in opportunity.eligible_years],
        required_skills=[
            RequiredSkillSummary(name=s.skill.name, minimum_proficiency=s.minimum_proficiency)
            for s in opportunity.required_skills
        ],
    )


def get_open_opportunities(session: Session) -> List[OpportunitySummary]:
    """Every currently OPEN opportunity, with its eligibility relations resolved.

    Status is filtered to OPEN at the repository level already; deadline
    filtering beyond that is a deterministic rule concern
    (app/rules/opportunity_eligibility.py), not this read-only service's job.
    """
    return [_opportunity_summary(o) for o in list_active_opportunities(session)]


def get_applications(session: Session, student_id: str) -> List[ApplicationSummary]:
    return [
        ApplicationSummary(opportunity_title=a.opportunity.title, status=a.status.value)
        for a in _get_applications(session, student_id)
    ]
