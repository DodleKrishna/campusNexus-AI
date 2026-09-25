"""Phase 15: routes a student's chat message to one read-only specialist agent.

The registered agents are used exactly as the Orchestrator uses them: one
``AgentMessage`` in, one verified ``AgentResult`` out. Only the four read-only
specialists can be reached here; the Action Agent cannot, so nothing said in
chat can write anything. The student comes from the caller (the JWT), never
from the message.

For the Events Agent, the student's current timetable and exams are passed
in as facts (read from ``app.services.academic``, deterministic DB reads), so
"will this clash with my schedule?" gets a real conflict check.
"""
from __future__ import annotations

import uuid
from typing import Dict, Optional

from sqlalchemy.orm import Session, sessionmaker

from app.db.tenant_session import TenantIsolationError, TenantSessionFactory

from app.graph.registry import AgentRegistry
from app.schemas.agent import AgentMessage
from app.schemas.agent_chat import SPECIALIST_AGENTS, SpecialistAnswer
from app.schemas.common import JsonValue
from app.schemas.enums import AgentName
from app.services import academic as academic_service


class ChatAgentError(Exception):
    """An agent key that is not a read-only specialist."""


class SpecialistGateway:
    def __init__(self, *, registry: AgentRegistry, session_factory: sessionmaker[Session]) -> None:
        self._registry = registry
        self._session_factory = session_factory

    def consult(self, agent_key: str, objective: str, *, student_id: str, organization_id: Optional[int] = None) -> SpecialistAnswer:
        agent_name = SPECIALIST_AGENTS.get(agent_key)
        if agent_name is None:
            raise ChatAgentError(f"{agent_key!r} is not a read-only specialist agent.")
        # Phase 22B: in the API the session is bound to the caller's organization (from its identity).
        if isinstance(self._session_factory, TenantSessionFactory):
            if organization_id is None:
                raise TenantIsolationError("a specialist consultation needs the caller's organization")
            session = self._session_factory.open_tenant_session(organization_id)
        else:
            session = self._session_factory()
        try:
            facts: Dict[str, JsonValue] = {"student_id": student_id, "query": objective}
            if agent_name == AgentName.EVENTS_OPPORTUNITY_AGENT:
                facts["timetable"] = [t.model_dump(mode="json") for t in academic_service.get_timetable(session, student_id)]
                facts["exams"] = [e.model_dump(mode="json") for e in academic_service.get_exam_schedule(session, student_id)]
            conversation = f"chat-{uuid.uuid4().hex[:12]}"
            message = AgentMessage(
                message_id=f"msg-{uuid.uuid4().hex[:12]}", mission_id=conversation, task_id=f"{conversation}-1",
                source=AgentName.MISSION_ORCHESTRATOR, target=agent_name, objective=objective, facts=facts,
            )
            outcome = self._registry.build(agent_name, session).handle(message)
        finally:
            session.close()
        return SpecialistAnswer(
            agent_key=agent_key, agent=agent_name.value, objective=objective,
            verification_status=outcome.verification.status.value, answer=outcome.response_text or "",
            facts=dict(outcome.agent_result.facts), evidence=list(outcome.agent_result.evidence),
            issues=list(outcome.verification.issues),
        )
