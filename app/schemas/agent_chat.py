"""Phase 15: the student agent-chat contract and the Enquiry Agent's structured plan.

UI agent keys are a mapping layer over the stable backend agent names --
``placements`` is the ``career_agent``, ``complaints`` is the
``campus_services_agent``, and so on. ``enquiry`` is the read-only Enquiry
Agent that consults the others.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.common import JsonValue
from app.schemas.enums import AgentName
from app.schemas.evidence import Evidence

SpecialistKey = Literal["academic", "events", "placements", "complaints"]
ChatAgentKey = Literal["academic", "events", "placements", "complaints", "enquiry", "permission"]

SPECIALIST_AGENTS: Dict[str, AgentName] = {
    "academic": AgentName.ACADEMIC_AGENT,
    "events": AgentName.EVENTS_OPPORTUNITY_AGENT,
    "placements": AgentName.CAREER_AGENT,
    "complaints": AgentName.CAMPUS_SERVICES_AGENT,
}
CHAT_AGENT_KEYS = (*SPECIALIST_AGENTS.keys(), "enquiry")
DISPLAY_NAMES = {
    "academic": "Academic Agent",
    "events": "Events Agent",
    "placements": "Placement Agent",
    "complaints": "Complaints Agent",
    "enquiry": "Enquiry Agent",
    "permission": "Permission Agent",
}


class AgentQueryRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class SpecialistAnswer(BaseModel):
    """One specialist agent's verified-or-not answer, exactly as it produced it."""

    agent_key: SpecialistKey
    agent: str
    objective: str
    verification_status: str  # verified / needs_review / failed
    answer: str = ""
    facts: Dict[str, JsonValue] = Field(default_factory=dict)
    evidence: List[Evidence] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)


class ActionHint(BaseModel):
    """Where an action request belongs. Chat never performs it."""

    agent_key: str
    message: str


class AgentQueryResponse(BaseModel):
    agent_key: ChatAgentKey
    display_name: str
    verification_status: str
    answer: str
    facts: Dict[str, JsonValue] = Field(default_factory=dict)
    evidence: List[Evidence] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)
    # Enquiry Agent only: every specialist consulted (verified or not) and the
    # sections of the answer built from the verified ones.
    consulted: List[SpecialistAnswer] = Field(default_factory=list)
    notices: List[str] = Field(default_factory=list)
    action_hint: Optional[ActionHint] = None
    live_ai: bool = False


# ---------------------------------------------------------------------------
# Enquiry Agent planning (LLM structured output)
# ---------------------------------------------------------------------------


class EnquiryConsultation(BaseModel):
    agent: SpecialistKey = Field(
        description=(
            "academic: attendance, exam eligibility, timetable, exam schedule, academic policy. "
            "events: campus events/workshops, schedule clashes, registration status. "
            "placements: internships/jobs eligibility, skill gaps, application status, deadlines. "
            "complaints: the student's grievance cases, SLA/overdue status, grievance procedure."
        )
    )
    objective: str = Field(
        min_length=3, max_length=300,
        description="A self-contained question for that agent, e.g. 'What is my timetable?'.",
    )


class EnquiryPlan(BaseModel):
    consultations: List[EnquiryConsultation] = Field(
        default_factory=list, max_length=6,
        description="The read-only questions to ask specialist agents. Empty if nothing is in scope.",
    )
    action_requested: bool = Field(
        default=False,
        description="True when the student asks to DO something (register, apply, file or cancel), not just to know.",
    )
    action_agent: Optional[SpecialistKey] = Field(
        default=None, description="Which specialist area the requested action belongs to, if any.",
    )
    asks_live_class_status: bool = Field(
        default=False,
        description=(
            "True when the student asks whether a class has actually started, whether attendance is being taken, "
            "whether they were marked present, or which class is happening now or next."
        ),
    )
    out_of_scope: Optional[str] = Field(
        default=None, description="Short note when part of the question is outside every specialist's scope.",
    )
