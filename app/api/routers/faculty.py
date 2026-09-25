"""/faculty/* (Phase 16): the signed-in faculty member's classes, attendance and agents.

The faculty member is resolved from the JWT's account link
(``current_faculty_profile``); no endpoint accepts a faculty id. Every class
id is checked against that faculty member's own teaching assignments inside
``app.services.faculty_ops`` (403 otherwise). Attendance writes are these
explicit endpoints only; agent chat here is read-only.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.services.ai_usage import record_no_ai
from app.api.ai import budget_exceeded, request_ai_context
from app.llm.router import AIBudgetExceededError
from app.db.tenant_session import session_organization
from app.agents.enquiry.faculty import FACULTY_AGENT_KEYS, FACULTY_DISPLAY_NAMES, FacultyAgent
from app.api.auth_deps import FacultyCaller, current_faculty_profile
from app.api.deps import get_knowledge_service, get_now, get_session
from app.llm.base import LLMProviderError, LLMTransientError
from app.schemas.agent_chat import AgentQueryRequest, AgentQueryResponse
from app.schemas.faculty import (
    AssignmentStanding,
    FacultyClassDetail,
    FacultyClassView,
    FacultyDashboard,
    FacultyProfileView,
    MarkAllRequest,
    MarkAttendanceRequest,
)
from app.schemas.department import StaffNotificationItem
from app.services import faculty_ops, staff_notifications
from app.services.faculty_ops import FacultyAccessError, OperationRefused
from app.services.knowledge import KnowledgeService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/faculty", tags=["faculty"])

_AGENT_DESCRIPTIONS = {
    "academic": "Your classes today, who is present or absent, and who is below the attendance requirement.",
    "enquiry": "Any question about your own classes and student requests, answered from live records. Read-only.",
    "permission": "Student permission, leave and OD requests waiting for your decision.",
}


class CancelBody(BaseModel):
    note: Optional[str] = Field(default=None, max_length=200)


class FacultyAgentItem(BaseModel):
    key: str
    display_name: str
    description: str
    available: bool = True


class FacultyAgentCatalog(BaseModel):
    live_ai: bool
    provider: str
    model: Optional[str]
    agents: List[FacultyAgentItem]


def _run(operation):
    try:
        return operation()
    except FacultyAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OperationRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/me", response_model=FacultyProfileView)
def me(caller: FacultyCaller = Depends(current_faculty_profile), session: Session = Depends(get_session)) -> FacultyProfileView:
    return faculty_ops.profile_view(session, caller.faculty)


@router.get("/dashboard", response_model=FacultyDashboard)
def dashboard(
    caller: FacultyCaller = Depends(current_faculty_profile), session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> FacultyDashboard:
    return faculty_ops.dashboard(session, caller.faculty, now)


@router.get("/classes/today", response_model=List[FacultyClassView])
def classes_today(
    caller: FacultyCaller = Depends(current_faculty_profile), session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> List[FacultyClassView]:
    return faculty_ops.classes_today(session, caller.faculty, now)


@router.get("/attendance", response_model=List[AssignmentStanding])
def attendance_overview(
    caller: FacultyCaller = Depends(current_faculty_profile), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now),
) -> List[AssignmentStanding]:
    return faculty_ops.attendance_overview(session, knowledge, caller.faculty, now)


@router.get("/classes/{session_id}", response_model=FacultyClassDetail)
def class_detail(
    session_id: int, caller: FacultyCaller = Depends(current_faculty_profile), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now),
) -> FacultyClassDetail:
    return _run(lambda: faculty_ops.class_detail(session, knowledge, caller.faculty, session_id, now))


def _detail_after(session, knowledge, caller, session_id, now) -> FacultyClassDetail:
    return faculty_ops.class_detail(session, knowledge, caller.faculty, session_id, now)


@router.post("/classes/{session_id}/start", response_model=FacultyClassDetail)
def start_class(
    session_id: int, caller: FacultyCaller = Depends(current_faculty_profile), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now),
) -> FacultyClassDetail:
    record_no_ai(session, "class_start", "deterministic class-session rules")
    _run(lambda: faculty_ops.start_class(session, caller.faculty, caller.account, session_id, now))
    return _detail_after(session, knowledge, caller, session_id, now)


@router.post("/classes/{session_id}/attendance", response_model=FacultyClassDetail)
def mark_attendance(
    session_id: int, body: MarkAttendanceRequest, caller: FacultyCaller = Depends(current_faculty_profile),
    session: Session = Depends(get_session), knowledge: KnowledgeService = Depends(get_knowledge_service),
    now: datetime = Depends(get_now),
) -> FacultyClassDetail:
    codes = [m.student_id for m in body.marks]
    if len(set(codes)) != len(codes):
        raise HTTPException(status_code=422, detail="Each student can be marked only once per request.")
    marks = {m.student_id: m.status for m in body.marks}
    _run(lambda: faculty_ops.mark_attendance(session, caller.faculty, caller.account, session_id, marks, now))
    return _detail_after(session, knowledge, caller, session_id, now)


@router.post("/classes/{session_id}/attendance/all", response_model=FacultyClassDetail)
def mark_all(
    session_id: int, body: MarkAllRequest, caller: FacultyCaller = Depends(current_faculty_profile),
    session: Session = Depends(get_session), knowledge: KnowledgeService = Depends(get_knowledge_service),
    now: datetime = Depends(get_now),
) -> FacultyClassDetail:
    _run(lambda: faculty_ops.mark_all(session, caller.faculty, caller.account, session_id, body.status, now))
    return _detail_after(session, knowledge, caller, session_id, now)


@router.post("/classes/{session_id}/close", response_model=FacultyClassDetail)
def close_class(
    session_id: int, caller: FacultyCaller = Depends(current_faculty_profile), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now),
) -> FacultyClassDetail:
    record_no_ai(session, "class_close", "deterministic attendance folding")
    _run(lambda: faculty_ops.close_class(session, caller.faculty, caller.account, session_id, now))
    return _detail_after(session, knowledge, caller, session_id, now)


@router.post("/classes/{session_id}/cancel", response_model=FacultyClassDetail)
def cancel_class(
    session_id: int, body: CancelBody, caller: FacultyCaller = Depends(current_faculty_profile),
    session: Session = Depends(get_session), knowledge: KnowledgeService = Depends(get_knowledge_service),
    now: datetime = Depends(get_now),
) -> FacultyClassDetail:
    _run(lambda: faculty_ops.cancel_class(session, caller.faculty, caller.account, session_id, now, body.note))
    return _detail_after(session, knowledge, caller, session_id, now)


@router.get("/notifications", response_model=List[StaffNotificationItem])
def notifications(caller: FacultyCaller = Depends(current_faculty_profile), session: Session = Depends(get_session)) -> List[StaffNotificationItem]:
    """Phase 17: the signed-in faculty member's (or HOD's) own in-app notifications."""
    return staff_notifications.list_for(session, caller.faculty.id)


# ---------------------------------------------------------------------------
# Faculty agents (read-only)
# ---------------------------------------------------------------------------


@router.get("/agents", response_model=FacultyAgentCatalog)
def agent_catalog(request: Request, _: FacultyCaller = Depends(current_faculty_profile)) -> FacultyAgentCatalog:
    provider = request.app.state.llm_provider
    return FacultyAgentCatalog(
        live_ai=bool(provider.is_live), provider=provider.name, model=provider.model_name,
        agents=[FacultyAgentItem(key=k, display_name=FACULTY_DISPLAY_NAMES[k], description=_AGENT_DESCRIPTIONS[k]) for k in FACULTY_AGENT_KEYS],
    )


@router.post("/agents/{agent_key}/query", response_model=AgentQueryResponse)
def agent_query(
    agent_key: str, body: AgentQueryRequest, request: Request, caller: FacultyCaller = Depends(current_faculty_profile),
    session: Session = Depends(get_session), knowledge: KnowledgeService = Depends(get_knowledge_service),
    now: datetime = Depends(get_now),
) -> AgentQueryResponse:
    if agent_key not in FACULTY_AGENT_KEYS:
        raise HTTPException(status_code=404, detail=f"There is no '{agent_key}' agent available to chat with.")
    agent = FacultyAgent(llm_provider=request.app.state.llm_provider, knowledge=knowledge)
    try:
        with request_ai_context(request, session_organization(session)):
            return agent.handle(session, caller.faculty, agent_key, body.message, now)
    except AIBudgetExceededError as exc:
        raise budget_exceeded(exc) from exc
    except LLMTransientError as exc:
        logger.warning("faculty chat: provider unavailable (%s)", exc.details())
        raise HTTPException(status_code=503, detail="Live AI is temporarily unavailable. Please try again shortly.") from exc
    except LLMProviderError as exc:
        logger.error("faculty chat: provider error: %s", exc)
        raise HTTPException(status_code=502, detail="The AI provider returned an unusable response. Please try again.") from exc
