"""Phase 15: JWT bearer authentication and authorization for the React application.

Independent of the Phase 8 ``X-Demo-Identity`` header (``app/api/deps.py``),
which the Streamlit debug UI and the existing tests still use. Every
``/auth``, ``/me`` and ``/agents`` endpoint derives the caller -- and, for a
student, the student_code -- from the verified token only. No endpoint in
this layer accepts a role or student id from the client.

Phase 22B: the token's ``org``/``mid`` claims are re-verified against the
database on every request (``app.auth.identity.resolve_identity``, one
statement), which also binds the request's tenant session to that
organization. The role, student and faculty profile come from the
organization membership, never from ``auth_accounts``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.accounts import get_account
from app.auth.identity import TenantContext, resolve_identity
from app.auth.tokens import TokenError, decode_token
from app.db.models.auth import AuthAccount
from app.db.models.faculty import FacultyProfile
from app.services.department_ops import HodScope
from app.schemas.enums import UserRole

_bearer = HTTPBearer(auto_error=False)
_CHALLENGE = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class AuthenticatedUser:
    account_id: int
    email: str
    role: UserRole  # the membership role (authoritative)
    display_name: str
    student_id: Optional[str]  # student_code of the membership's student, if any
    department_id: Optional[int]
    tenant: TenantContext
    faculty_profile_id: Optional[int] = None  # the membership's faculty profile, if any

    @property
    def organization_id(self) -> int:
        return self.tenant.organization_id


def require_authenticated_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    session: Session = Depends(get_session),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Sign in to continue.", headers=_CHALLENGE)
    try:
        claims = decode_token(credentials.credentials)
    except TokenError as exc:
        detail = "Your session expired. Please sign in again." if str(exc) == "expired" else "Invalid session. Please sign in again."
        raise HTTPException(status_code=401, detail=detail, headers=_CHALLENGE) from exc
    identity = resolve_identity(session, account_id=claims.account_id, organization_id=claims.organization_id,
                                membership_id=claims.membership_id)
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid session. Please sign in again.", headers=_CHALLENGE)
    return authenticated_user(identity)


def authenticated_user(identity) -> AuthenticatedUser:
    account, student, faculty = identity.account, identity.student, identity.faculty
    department_id = student.department_id if student else faculty.department_id if faculty else account.department_id
    return AuthenticatedUser(
        account_id=account.id, email=account.email, role=identity.context.role, display_name=account.display_name,
        student_id=student.student_code if student else None, department_id=department_id, tenant=identity.context,
        faculty_profile_id=faculty.id if faculty else None,
    )


def require_roles(*roles: UserRole) -> Callable[..., AuthenticatedUser]:
    allowed = set(roles)

    def _dependency(user: AuthenticatedUser = Depends(require_authenticated_user)) -> AuthenticatedUser:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="Your role does not have access to this.")
        return user

    return _dependency


def current_student(user: AuthenticatedUser = Depends(require_roles(UserRole.STUDENT))) -> str:
    """The signed-in student's own student_code -- the only student these endpoints serve."""
    if not user.student_id:
        raise HTTPException(status_code=403, detail="This account is not linked to a student record.")
    return user.student_id


def current_faculty(user: AuthenticatedUser = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))) -> AuthenticatedUser:
    return user


@dataclass(frozen=True)
class FacultyCaller:
    """The signed-in faculty member: their account and the faculty profile it is linked to."""

    account: AuthAccount
    faculty: FacultyProfile


def current_faculty_profile(
    user: AuthenticatedUser = Depends(current_faculty), session: Session = Depends(get_session),
) -> FacultyCaller:
    """Phase 16/22B: the faculty profile comes only from the organization membership (tenant-scoped)."""
    account = get_account(session, user.account_id)
    faculty = session.get(FacultyProfile, user.faculty_profile_id) if user.faculty_profile_id else None
    if account is None or faculty is None:
        raise HTTPException(status_code=403, detail="This account is not linked to a faculty profile.")
    return FacultyCaller(account=account, faculty=faculty)


def current_student_account(
    user: AuthenticatedUser = Depends(require_roles(UserRole.STUDENT)), session: Session = Depends(get_session),
) -> AuthAccount:
    account = get_account(session, user.account_id)
    if account is None or not user.student_id:
        raise HTTPException(status_code=403, detail="This account is not linked to a student record.")
    return account


def current_hod(
    user: AuthenticatedUser = Depends(require_roles(UserRole.HOD)), session: Session = Depends(get_session),
) -> "HodScope":
    """Phase 17: HOD role AND a linked faculty profile AND that profile heads its department.

    The department is derived here, never taken from the client."""
    from app.services.department_ops import hod_scope

    scope = hod_scope(session, user.account_id, user.faculty_profile_id)
    if scope is None:
        raise HTTPException(status_code=403, detail="This account is not the recorded head of a department.")
    return scope
