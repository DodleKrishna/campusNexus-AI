"""GET /agents, POST /agents/{agent_key}/query (Phase 15) -- student agent chat.

Students only; the student is taken from the JWT. Only the four read-only
specialists and the read-only Enquiry Agent are reachable, so chat can never
write anything. Provider problems become clean messages, never tracebacks.
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.agents.enquiry.agent import EnquiryAgent
from app.api.auth_deps import current_student
from app.llm.base import LLMProviderError, LLMTransientError
from app.schemas.agent_chat import (
    CHAT_AGENT_KEYS,
    DISPLAY_NAMES,
    SPECIALIST_AGENTS,
    AgentQueryRequest,
    AgentQueryResponse,
)
from app.services.agent_chat import SpecialistGateway

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])

_DESCRIPTIONS = {
    "academic": "Attendance, exam eligibility, timetable and exam schedule, grounded in academic policy.",
    "events": "Campus events and workshops, checked against your classes and exams.",
    "placements": "Internship eligibility, skill gaps and application status.",
    "complaints": "Your grievance cases, SLA status and the escalation procedure.",
    "enquiry": "General campus questions answered by consulting the other agents. Read-only.",
    "permission": "Leave and permission requests with faculty approval.",
}


class AgentCatalogItem(BaseModel):
    key: str
    display_name: str
    description: str
    backend_agent: str | None
    available: bool


class AgentCatalog(BaseModel):
    live_ai: bool
    provider: str
    model: str | None
    agents: List[AgentCatalogItem]


def _gateway(request: Request) -> SpecialistGateway:
    return request.app.state.specialist_gateway


@router.get("", response_model=AgentCatalog)
def catalog(request: Request, _: str = Depends(current_student)) -> AgentCatalog:
    provider = request.app.state.llm_provider
    items = [
        AgentCatalogItem(
            key=key, display_name=DISPLAY_NAMES[key], description=_DESCRIPTIONS[key],
            backend_agent=SPECIALIST_AGENTS[key].value if key in SPECIALIST_AGENTS else None, available=True,
        )
        for key in CHAT_AGENT_KEYS
    ]
    items.append(AgentCatalogItem(
        key="permission", display_name="Permission Agent", description=_DESCRIPTIONS["permission"],
        backend_agent=None, available=False,
    ))
    return AgentCatalog(live_ai=bool(provider.is_live), provider=provider.name, model=provider.model_name, agents=items)


@router.post("/{agent_key}/query", response_model=AgentQueryResponse)
def query(
    agent_key: str,
    body: AgentQueryRequest,
    request: Request,
    student_id: str = Depends(current_student),
    gateway: SpecialistGateway = Depends(_gateway),
) -> AgentQueryResponse:
    if agent_key not in CHAT_AGENT_KEYS:
        raise HTTPException(status_code=404, detail=f"There is no '{agent_key}' agent available to chat with.")
    provider = request.app.state.llm_provider
    try:
        if agent_key == "enquiry":
            agent = EnquiryAgent(
                llm_provider=provider,
                consult=lambda key, objective: gateway.consult(key, objective, student_id=student_id),
            )
            return agent.handle(body.message)
        answer = gateway.consult(agent_key, body.message, student_id=student_id)
    except LLMTransientError as exc:
        logger.warning("agent chat: provider unavailable (%s)", exc.details())
        raise HTTPException(status_code=503, detail="Live AI is temporarily unavailable. Please try again shortly.") from exc
    except LLMProviderError as exc:
        logger.error("agent chat: provider error: %s", exc)
        raise HTTPException(status_code=502, detail="The AI provider returned an unusable response. Please try again.") from exc
    return AgentQueryResponse(
        agent_key=agent_key, display_name=DISPLAY_NAMES[agent_key], verification_status=answer.verification_status,
        answer=answer.answer, facts=answer.facts, evidence=answer.evidence, issues=answer.issues,
        live_ai=bool(provider.is_live),
    )
