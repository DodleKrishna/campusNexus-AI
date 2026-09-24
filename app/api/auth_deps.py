"""Phase 15: JWT bearer authentication and authorization for the React application.

Independent of the Phase 8 ``X-Demo-Identity`` header (``app/api/deps.py``),
which the Streamlit debug UI and the existing tests still use. Every
``/auth``, ``/me`` and ``/agents`` endpoint derives the caller -- and, for a
student, the student_code -- from the verified token only. No endpoint in
this layer accepts a role or student id from the client.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.accounts import get_account
from app.auth.tokens import TokenError, decode_token
from app.schemas.enums import UserRole

_bearer = HTTPBearer(auto_error=False)
_CHALLENGE = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class AuthenticatedUser:
    account_id: int
    email: str
    role: UserRole
    display_name: str
    student_id: Optional[str]
    department_id: Optional[int]


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
    account = get_account(session, claims.account_id)
    if account is None or not account.is_active:
        raise HTTPException(status_code=401, detail="Invalid session. Please sign in again.", headers=_CHALLENGE)
    return AuthenticatedUser(
        account_id=account.id, email=account.email, role=account.role, display_name=account.display_name,
        student_id=account.linked_student_id, department_id=account.department_id,
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
