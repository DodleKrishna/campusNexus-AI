"""AgentOS catalog (hackathon demo surface): the product agents an institution can deploy.

Static default configuration. Product agents are capabilities in front of the
orchestrator; the Action Agent, Deterministic Verifier and Approval Gate are
internal infrastructure and are never deployable or directly reachable.
"""
from __future__ import annotations

from app.schemas.admin_console import AgentCatalogView, CatalogAgent
from app.schemas.enums import IntelligenceLevel

_L, _A, _N = IntelligenceLevel.LIGHT.value, IntelligenceLevel.ADVANCED.value, IntelligenceLevel.NO_AI.value

PRODUCT_AGENTS = [
    CatalogAgent(key="academic", name="Academic Agent", purpose="Coursework, attendance standing, exams and academic policy answers with cited evidence.",
                 status="active", intelligence_level=_A, allowed_roles=["student", "faculty", "hod", "admin"], requires_approval=False),
    CatalogAgent(key="attendance", name="Attendance Agent", purpose="Live class sessions, marking and eligibility from deterministic attendance rules.",
                 status="active", intelligence_level=_N, allowed_roles=["student", "faculty", "hod"], requires_approval=False),
    CatalogAgent(key="career", name="Career / Placement Agent", purpose="Internships and jobs matched to the student's skills, with skill-gap guidance.",
                 status="active", intelligence_level=_A, allowed_roles=["student"], requires_approval=False),
    CatalogAgent(key="events", name="Events Agent", purpose="Events, clubs and competitions; registrations are proposed and need approval.",
                 status="active", intelligence_level=_L, allowed_roles=["student", "hod", "admin"], requires_approval=True),
    CatalogAgent(key="campus_services", name="Campus Services Agent", purpose="Hostel, fees, facilities and complaints with SLA tracking; filing a case needs approval.",
                 status="active", intelligence_level=_L, allowed_roles=["student", "hod", "admin"], requires_approval=True),
    CatalogAgent(key="enquiry", name="Enquiry Agent", purpose="One question in, the right specialists consulted; multi-step goals become planned missions.",
                 status="active", intelligence_level=_L, allowed_roles=["student", "faculty", "hod", "admin"], requires_approval=False),
]

INTERNAL_COMPONENTS = [
    CatalogAgent(key="action", name="Action Agent", purpose="The only component that executes side-effecting tools.",
                 status="internal", intelligence_level=_N, allowed_roles=[], requires_approval=True, deployable=False),
    CatalogAgent(key="verifier", name="Deterministic Verifier", purpose="Checks every plan and action against official rules, before and after.",
                 status="internal", intelligence_level=_N, allowed_roles=[], requires_approval=False, deployable=False),
    CatalogAgent(key="approval_gate", name="Approval Gate", purpose="Human approval for anything irreversible, financial or sent to others.",
                 status="internal", intelligence_level=_N, allowed_roles=[], requires_approval=False, deployable=False),
]

FLOW = ["User", "Agent capability", "Mission Orchestrator", "Specialist agent", "Controlled tools",
        "Action Agent", "Verifier", "Approval Gate (when sensitive)"]


def catalog() -> AgentCatalogView:
    return AgentCatalogView(agents=PRODUCT_AGENTS, internal_components=INTERNAL_COMPONENTS, flow=FLOW)


TEMPLATES = {agent.key: agent for agent in PRODUCT_AGENTS}
INTERNAL_KEYS = frozenset({"action", "action_agent", "verifier", "approval_gate"})
