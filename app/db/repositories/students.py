"""Read-only student/identity repository functions."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.identity import Department, Student, User


def get_student_by_id(session: Session, student_code: str) -> Optional[Student]:
    """Look up a student by their stable natural key (e.g. "STU-DEMO-001")."""
    stmt = select(Student).where(Student.student_code == student_code)
    return session.execute(stmt).scalar_one_or_none()


def get_department_by_code(session: Session, code: str) -> Optional[Department]:
    stmt = select(Department).where(Department.code == code)
    return session.execute(stmt).scalar_one_or_none()


def get_user_by_email(session: Session, email: str) -> Optional[User]:
    stmt = select(User).where(User.email == email)
    return session.execute(stmt).scalar_one_or_none()


def list_students_by_department(session: Session, department_code: str) -> list[Student]:
    stmt = (
        select(Student)
        .join(Department, Student.department_id == Department.id)
        .where(Department.code == department_code)
    )
    return list(session.execute(stmt).scalars().all())
