"""Account lookup, authentication and development-account seeding.

Seeded accounts are for local development and demos only. Their initial
password is never committed: it comes from ``CAMPUSNEXUS_DEMO_PASSWORD``, or
the caller (``scripts/reset_demo_env.py``) generates a random one and tells
the operator.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password, verify_password
from app.db.base import utc_now
from app.db.models.auth import AuthAccount
from app.db.models.faculty import FacultyProfile
from app.db.models.identity import Department, Student
from app.schemas.enums import UserRole

DEMO_PASSWORD_ENV = "CAMPUSNEXUS_DEMO_PASSWORD"


@dataclass(frozen=True)
class SeedAccount:
    email: str
    role: UserRole
    display_name: str
    student_code: Optional[str] = None
    department_code: Optional[str] = None
    # Phase 16: faculty_profiles.employee_code for FACULTY/HOD accounts.
    faculty_code: Optional[str] = None


# Faculty/HOD accounts are linked to the seeded faculty profiles of real course
# instructors (scripts/seed_data.py FACULTY_SEED). faculty@ is Dr. Ashok Verma,
# who teaches Computer Networks to Aditi's section and is its mentor; the other
# semester-5 CSE instructors can sign in too, so any routed request can be decided.
DEV_ACCOUNTS: List[SeedAccount] = [
    SeedAccount("student@campusnexus.local", UserRole.STUDENT, "Aditi Rao", student_code="STU-DEMO-001", department_code="CSE"),
    SeedAccount("faculty@campusnexus.local", UserRole.FACULTY, "Dr. Ashok Verma", department_code="CSE", faculty_code="EMP-CSE-004"),
    SeedAccount("hod@campusnexus.local", UserRole.HOD, "Dr. Kavita Iyer", department_code="CSE", faculty_code="EMP-CSE-001"),
    SeedAccount("admin@campusnexus.local", UserRole.ADMIN, "Priya Desai"),
    SeedAccount("manoj.pillai@campusnexus.local", UserRole.FACULTY, "Dr. Manoj Pillai", department_code="CSE", faculty_code="EMP-CSE-002"),
    SeedAccount("sunita.rao@campusnexus.local", UserRole.FACULTY, "Dr. Sunita Rao", department_code="CSE", faculty_code="EMP-CSE-003"),
    SeedAccount("leela.nair@campusnexus.local", UserRole.FACULTY, "Dr. Leela Nair", department_code="CSE", faculty_code="EMP-CSE-005"),
]


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_account(session: Session, account_id: int) -> Optional[AuthAccount]:
    return session.get(AuthAccount, account_id)


def get_account_by_email(session: Session, email: str) -> Optional[AuthAccount]:
    return session.execute(
        select(AuthAccount).where(func.lower(AuthAccount.email) == normalize_email(email))
    ).scalar_one_or_none()


def authenticate(session: Session, email: str, password: str) -> Optional[AuthAccount]:
    """The active account for these credentials, or None. Unknown email and wrong
    password are indistinguishable to the caller."""
    account = get_account_by_email(session, email)
    if account is None:
        verify_password(password, "$2b$12$" + "." * 53)  # similar cost either way
        return None
    if not account.is_active or not verify_password(password, account.password_hash):
        return None
    account.last_login_at = utc_now()
    session.commit()
    return account


def resolve_seed_password(credentials_file: Path) -> Tuple[str, str]:
    """The development-account password and where it came from.

    ``CAMPUSNEXUS_DEMO_PASSWORD`` wins. Otherwise a password saved earlier in
    ``credentials_file`` (git-ignored, local only) is reused, so a reset keeps
    the same sign-in. Otherwise a random one is generated and saved there.
    Phase 19: the file is always rewritten with the password actually seeded,
    so it can never show a stale password after an env-var reset.
    """
    configured = os.environ.get(DEMO_PASSWORD_ENV)
    if configured:
        _write_credentials(credentials_file, configured, source=f"${DEMO_PASSWORD_ENV}")
        return configured, f"${DEMO_PASSWORD_ENV}"
    if credentials_file.exists():
        for line in credentials_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("password:"):
                password = line.split(":", 1)[1].strip()
                _write_credentials(credentials_file, password)
                return password, str(credentials_file)
    password = secrets.token_urlsafe(12)
    _write_credentials(credentials_file, password)
    return password, str(credentials_file)


def _write_credentials(credentials_file: Path, password: str, source: Optional[str] = None) -> None:
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# CampusNexus local development accounts (never commit this file)"]
    if source:
        lines.append(f"# password set by {source} at the last reset")
    lines.append(f"password: {password}")
    lines += [f"account: {spec.email} ({spec.role.value})" for spec in DEV_ACCOUNTS]
    credentials_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _faculty_id(session: Session, employee_code: Optional[str]) -> Optional[int]:
    if not employee_code:
        return None
    return session.execute(
        select(FacultyProfile.id).where(FacultyProfile.employee_code == employee_code)
    ).scalar_one_or_none()


def seed_dev_accounts(session: Session, password: str) -> List[str]:
    """Create the development accounts that don't exist yet (idempotent; an
    existing account's password is never changed). Returns created emails.
    A faculty account created before its profile existed is linked now."""
    created: List[str] = []
    for spec in DEV_ACCOUNTS:
        faculty_id = _faculty_id(session, spec.faculty_code)
        existing = get_account_by_email(session, spec.email)
        if existing is not None:
            if existing.linked_faculty_id is None and faculty_id is not None:
                existing.linked_faculty_id = faculty_id
            continue
        department_id = None
        if spec.department_code:
            department = session.execute(select(Department).where(Department.code == spec.department_code)).scalar_one_or_none()
            department_id = department.id if department else None
        if spec.student_code and session.execute(
            select(Student).where(Student.student_code == spec.student_code)
        ).scalar_one_or_none() is None:
            continue  # the student must be seeded first
        session.add(AuthAccount(
            email=spec.email, password_hash=hash_password(password), role=spec.role, display_name=spec.display_name,
            linked_student_id=spec.student_code, linked_faculty_id=faculty_id, department_id=department_id, is_active=True,
        ))
        created.append(spec.email)
    session.commit()
    return created
