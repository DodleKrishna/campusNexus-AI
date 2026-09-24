"""POST /auth/login, GET /auth/me, POST /auth/logout (Phase 15)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth_deps import AuthenticatedUser, require_authenticated_user
from app.api.deps import get_session
from app.auth.accounts import authenticate
from app.auth.tokens import issue_token
from app.db.repositories import operations_audit
from app.db.models.identity import Department

router = APIRouter(prefix="/auth", tags=["auth"])

# Where each role lands after signing in (the React router mirrors this).
HOME_ROUTES = {"student": "/student", "faculty": "/faculty", "hod": "/hod", "admin": "/admin", "staff": "/admin"}


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class AuthUserView(BaseModel):
    id: int
    email: str
    role: str
    display_name: str
    student_id: Optional[str] = None
    department_code: Optional[str] = None
    department_name: Optional[str] = None
    home_route: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: AuthUserView


def _user_view(session: Session, user: AuthenticatedUser) -> AuthUserView:
    department = session.get(Department, user.department_id) if user.department_id else None
    return AuthUserView(
        id=user.account_id, email=user.email, role=user.role.value, display_name=user.display_name,
        student_id=user.student_id, department_code=department.code if department else None,
        department_name=department.name if department else None, home_route=HOME_ROUTES.get(user.role.value, "/"),
    )


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, session: Session = Depends(get_session)) -> LoginResponse:
    account = authenticate(session, body.email, body.password)
    if account is None:
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    # Phase 18: successful sign-ins are audited (never the password, never failed-attempt details).
    operations_audit.record(
        session, event_type="login", actor_account_id=account.id, actor_role=account.role.value,
        subject_type="auth_account", subject_id=str(account.id), message=f"{account.display_name} signed in.",
    )
    session.commit()
    token, expires_at = issue_token(account.id, account.role.value)
    user = AuthenticatedUser(
        account_id=account.id, email=account.email, role=account.role, display_name=account.display_name,
        student_id=account.linked_student_id, department_id=account.department_id,
    )
    return LoginResponse(access_token=token, expires_at=expires_at, user=_user_view(session, user))


@router.get("/me", response_model=AuthUserView)
def me(user: AuthenticatedUser = Depends(require_authenticated_user), session: Session = Depends(get_session)) -> AuthUserView:
    return _user_view(session, user)


@router.post("/logout", status_code=204)
def logout(_: AuthenticatedUser = Depends(require_authenticated_user)) -> Response:
    """Tokens are stateless: the client discards its token. The endpoint exists
    so the client has one explicit sign-out call (and a place for revocation later)."""
    return Response(status_code=204)
