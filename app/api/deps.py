"""FastAPI dependency injection: DB sessions, shared backend services, and
demo-identity resolution/authorization (Phase 8 §3).

Every dependency here either reads a per-request resource (``get_session``)
or a singleton built once at app startup and stored on ``app.state`` by
``app.api.main.create_app`` (``get_orchestrator``/``get_tool_gateway``/
``get_knowledge_service``) -- nothing is rebuilt per request.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.identities import DEMO_IDENTITIES
from app.db.repositories.students import get_student_by_id
from app.graph.orchestrator import MissionOrchestrator
from app.schemas.enums import UserRole
from app.services.knowledge import KnowledgeService
from app.tools.registry import ToolGateway


@dataclass(frozen=True)
class Identity:
    """A resolved demo identity -- never built from client-supplied role/student_id."""

    key: str
    role: UserRole
    student_id: Optional[str]
    display_name: str


def get_session(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_orchestrator(request: Request) -> MissionOrchestrator:
    return request.app.state.orchestrator


def get_tool_gateway(request: Request) -> ToolGateway:
    return request.app.state.tool_gateway


def get_knowledge_service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge_service


def get_now(request: Request) -> datetime:
    """Phase 16: the current instant (timezone-aware UTC) from the app's clock, so
    tests can pin time for class-session rules without touching the system clock."""
    return request.app.state.clock()


def get_identity(
    x_demo_identity: Optional[str] = Header(default=None, alias="X-Demo-Identity"),
    session: Session = Depends(get_session),
) -> Identity:
    """Resolve the caller's identity strictly from the server-side allowlist.

    A missing/unknown header is 401. This is the *only* place a request's
    role/student_id is ever established -- no endpoint accepts either as a
    trusted client-supplied value.
    """
    if not x_demo_identity or x_demo_identity not in DEMO_IDENTITIES:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing or unknown X-Demo-Identity header. This is a local demo application with no "
                "production authentication -- see docs/ARCHITECTURE.md's Phase 8 trust-boundary note."
            ),
        )
    definition = DEMO_IDENTITIES[x_demo_identity]
    if definition.student_id is not None:
        student = get_student_by_id(session, definition.student_id)
        if student is None:
            raise HTTPException(
                status_code=500,
                detail=f"Demo identity {definition.key!r} references an unseeded student_id {definition.student_id!r}.",
            )
        display_name = f"{student.user.full_name} (Student)"
    else:
        display_name = definition.fallback_display_name
    return Identity(key=definition.key, role=definition.role, student_id=definition.student_id, display_name=display_name)


def require_role(*roles: UserRole):
    """A dependency factory: 403s unless the caller's identity has one of ``roles``."""

    def _check(identity: Identity = Depends(get_identity)) -> Identity:
        if identity.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"role {identity.role.value!r} is not authorized for this operation.",
            )
        return identity

    return _check


def require_student_ownership(identity: Identity, path_student_id: str) -> None:
    """A STUDENT identity may only ever read/act on their own student_id;
    ADMIN/FACULTY may access any student's data (staff/support view)."""
    if identity.role == UserRole.STUDENT and identity.student_id != path_student_id:
        raise HTTPException(status_code=403, detail="students may only access their own data.")
