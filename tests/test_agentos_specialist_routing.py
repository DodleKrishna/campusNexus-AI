"""Phase 6.4: Nexus reaches the legacy read-only domain specialists through the existing SpecialistGateway.

Offline: default mock brain (deterministic keyword route) and the mock LLM provider behind the real
Academic / Career / Events / Campus Services agents. The Action Agent is never reachable.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.agentos.nexus import CONSULT_SPECIALIST_TOOL, NEXUS_AGENT_KEY, ConsultSpecialistInput
from app.agentos.providers import mock_specialist_route
from app.db.models import AgentDeployment
from app.schemas.agent_chat import SPECIALIST_AGENTS
from tests.test_agentos_nexus import ask, mission_rows, use_groq, wire
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_FACULTY, A_STUDENT, B_STUDENT, app, bearer, client, login, orgs,
)

ROUTES = [
    ("Show my timetable", "academic", "academic_agent"),
    ("Show placement opportunities", "placements", "career_agent"),
    ("What events are coming up?", "events", "events_opportunity_agent"),
    ("What is the hostel policy?", "complaints", "campus_services_agent"),
    ("Explain attendance policy", "academic", "academic_agent"),
    ("Register me for this event", "events", "events_opportunity_agent"),
]


@pytest.mark.parametrize("message,key,_agent", ROUTES)
def test_mock_route_table(message, key, _agent) -> None:
    assert mock_specialist_route(message) == key
    assert mock_specialist_route("Who am I and what can you help me with?") is None


def test_the_tool_offers_only_the_read_only_specialists() -> None:
    assert set(ConsultSpecialistInput.model_json_schema()["properties"]["specialist"]["enum"]) == set(SPECIALIST_AGENTS)
    for bad in ({"specialist": "action", "question": "x"}, {"specialist": "academic", "question": "x", "student_id": "S"}):
        with pytest.raises(ValidationError):
            ConsultSpecialistInput.model_validate(bad)


def test_nexus_registers_the_specialist_tool(app) -> None:
    runtime = app.state.agent_runtime
    assert CONSULT_SPECIALIST_TOOL in runtime.agents.get(NEXUS_AGENT_KEY).allowed_tools
    assert runtime.tools.get(CONSULT_SPECIALIST_TOOL) is not None


@pytest.mark.parametrize("message,key,agent", ROUTES)
def test_nexus_routes_domain_requests_to_the_existing_specialist(client, session_factory, message, key, agent) -> None:
    reply = ask(client, A_STUDENT, message)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["status"] == "completed" and body["assistant_message"]
    _, steps, audits = mission_rows(session_factory, body["mission_id"])
    assert [s.tool_name for s in steps] == [CONSULT_SPECIALIST_TOOL, None]
    output = steps[0].output_summary
    assert output["ok"] is True, output
    assert (output["data"]["specialist"], output["data"]["agent"]) == (key, agent)
    assert output["data"]["verification_status"] in {"verified", "needs_review", "failed"}
    assert "TOOL_EXECUTED" in [a.event_type for a in audits]


def test_a_live_brain_routes_by_the_schema_through_the_same_tool(app, client, session_factory) -> None:
    use_groq(app, [
        wire("tool", tool_name=CONSULT_SPECIALIST_TOOL,
             tool_input_json='{"specialist": "events", "question": "What events are coming up?"}'),
        wire("complete", outcome="Answered.", user_message="Here are the upcoming events."),
    ])
    body = ask(client, A_STUDENT, "What events are coming up?").json()
    _, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert steps[0].output_summary["data"]["agent"] == "events_opportunity_agent"


def test_staff_are_not_offered_the_student_scoped_specialist_tool(client, session_factory) -> None:
    body = ask(client, A_FACULTY, "Show my timetable").json()
    _, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert CONSULT_SPECIALIST_TOOL not in [s.tool_name for s in steps]


def test_a_paused_deployment_is_refused_not_answered(client, session_factory, orgs) -> None:
    with session_factory() as s:
        s.execute(select(AgentDeployment).where(AgentDeployment.organization_id == orgs["a"],
                                                AgentDeployment.agent_key == "career")).scalar_one().status = "paused"
        s.commit()
    body = ask(client, A_STUDENT, "Show placement opportunities").json()
    _, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert (steps[0].tool_name, steps[0].output_summary["error_code"]) == (CONSULT_SPECIALIST_TOOL, "AGENT_DISABLED")
    assert "not available" in body["assistant_message"]


def test_an_organization_without_the_deployment_is_refused(client, session_factory) -> None:
    body = ask(client, B_STUDENT, "Show my timetable").json()
    _, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert steps[0].output_summary["error_code"] == "AGENT_NOT_DEPLOYED"


def test_a_registration_request_only_consults_and_never_writes(client, session_factory) -> None:
    from app.db.models import ApprovalRecord, EventRegistration
    with session_factory() as s:
        before = (s.query(EventRegistration).count(), s.query(ApprovalRecord).count())
    body = ask(client, A_STUDENT, "Register me for the Competitive Coding Contest event").json()
    _, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert [s.tool_name for s in steps] == [CONSULT_SPECIALIST_TOOL, None]
    with session_factory() as s:
        assert (s.query(EventRegistration).count(), s.query(ApprovalRecord).count()) == before


def test_the_route_reaches_the_brain_through_the_tool_description() -> None:
    """Brains get the tool description (<= 200 chars) and a compact input schema without field descriptions."""
    from app.agentos.nexus import ConsultDomainSpecialist
    text = ConsultDomainSpecialist.description
    assert len(text) <= 200
    for key in SPECIALIST_AGENTS:
        assert f"{key}:" in text
