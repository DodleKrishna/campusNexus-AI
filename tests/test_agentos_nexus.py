"""AgentOS V2 Phase 2: provider-backed AgentBrain + the personal Nexus assistant.

No real provider is called: Groq is replaced by a scripted double (or a real
``GroqLLMProvider`` over a fake HTTP client), and the default app uses the offline mock brain.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app.agentos.nexus import NEXUS_AGENT_KEY, GetMyIdentityContext
from app.agentos.providers import (
    MAX_PROMPT_CHARS, SYSTEM_PROMPT, GroqAgentBrain, UnavailableBrain, brain_payload, build_agent_brain, decision_schema,
    parse_decision,
)
from app.agentos.brain import BrainOutputError
from app.agentos.runtime import RECENT_OBSERVATIONS, AgentRuntime
from app.agentos.schemas import AgentContext, AgentObservation, DecisionKind, ToolDescriptor
from app.db.models import AgentMission, AgentMissionStatus, AgentStep, AgentStepStatus, AuthAccount, OperationAuditEvent
from app.db.models.ai_usage import AIUsageEvent
from app.db.models.faculty import FacultyProfile
from app.db.models.identity import Student
from app.db.models.organization import Organization, OrganizationMembership
from app.llm.base import LLMProviderError, LLMTransientError
from app.llm.providers.groq import GroqLLMProvider
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_FACULTY, A_STUDENT, B_ADMIN, B_STUDENT, app, bearer, client, login, orgs,
)

PHONE = "+91 98765 43210"
SECRET = "hunter2-very-secret"
FIELDS = ("kind", "tool_name", "tool_input_json", "question", "outcome", "reason", "user_message")


def wire(kind: str, **values) -> dict:
    """A strict-schema answer as Groq returns it: every field present, unused ones null."""
    return {**{f: None for f in FIELDS}, "kind": kind, **values}


IDENTITY = wire("tool", tool_name="get_my_identity_context", tool_input_json="{}")
DONE = wire("complete", outcome="Answered.", user_message="You are signed in. Other capabilities are not available yet.")


class FakeGroq:
    """Stands in for GroqLLMProvider.complete_json_schema: scripted answers or exceptions, usage reported."""

    model_name = "openai/gpt-oss-20b"

    def __init__(self, script) -> None:
        self.script, self.calls, self._usage = list(script), [], None

    def complete_json_schema(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        self._usage = (120, 30)
        return item

    def take_last_usage(self):
        usage, self._usage = self._usage, None
        return usage


def use_brain(app, brain):
    runtime = app.state.agent_runtime
    app.state.agent_runtime = AgentRuntime(runtime.agents, runtime.tools, brain, clock=runtime.clock)
    return brain


def use_groq(app, script) -> FakeGroq:
    fake = FakeGroq(script)
    use_brain(app, GroqAgentBrain(fake, recorder=app.state.llm_provider.recorder))
    return fake


def ask(client, email, message="Who am I and what can you help me with?", **extra):
    return client.post("/agentos/assistant/message", json={"message": message, **extra},
                       headers=bearer(login(client, email)["access_token"]))


def mission_rows(session_factory, mission_id):
    with session_factory() as s:
        mission = s.get(AgentMission, mission_id)
        steps = list(s.execute(select(AgentStep).where(AgentStep.mission_id == mission_id)
                               .order_by(AgentStep.step_number)).scalars())
        audits = list(s.execute(select(OperationAuditEvent).where(
            OperationAuditEvent.subject_type == "agent_mission", OperationAuditEvent.subject_id == str(mission_id))
            .order_by(OperationAuditEvent.id)).scalars())
        s.expunge_all()
        return mission, steps, audits


def account_id(session_factory, email):
    with session_factory() as s:
        return s.execute(select(AuthAccount.id).where(AuthAccount.email == email)).scalar_one()


# --- Nexus through the API (default offline mock brain) -------------------------------------------------------------


def test_a_student_request_creates_the_students_own_nexus_mission(client, session_factory) -> None:
    reply = ask(client, A_STUDENT)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert (body["status"], body["steps_performed"], body["brain"]) == ("completed", 2, {"provider": "mock", "model": None, "live": False})
    assert "Offline mock" in body["assistant_message"]
    mission, steps, audits = mission_rows(session_factory, body["mission_id"])
    assert (mission.agent_key, mission.owner_account_id) == (NEXUS_AGENT_KEY, account_id(session_factory, A_STUDENT))
    assert [s.tool_name for s in steps] == ["get_my_identity_context", None]
    identity = steps[0].output_summary["data"]
    assert identity["role"] == "student" and "student" in identity and "faculty" not in identity
    kinds = [a.event_type for a in audits]
    assert kinds[:2] == ["MISSION_CREATED", "NEXUS_REQUEST_RECEIVED"] and "MISSION_COMPLETED" in kinds
    assert kinds.count("AI_BRAIN_CALLED") == kinds.count("AI_BRAIN_SUCCEEDED") == 2


def test_a_faculty_request_creates_the_faculty_members_own_mission(client, session_factory) -> None:
    body = ask(client, A_FACULTY).json()
    mission, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert body["status"] == "completed" and mission.owner_account_id == account_id(session_factory, A_FACULTY)
    identity = steps[0].output_summary["data"]
    assert identity["role"] == "faculty" and "faculty" in identity and "student" not in identity


def test_identity_fields_in_the_body_are_refused(client, session_factory, orgs) -> None:
    other = account_id(session_factory, B_STUDENT)
    for extra in ({"organization_id": orgs["b"]}, {"account_id": other}, {"role": "admin"}, {"student_id": "STU-X"}):
        assert ask(client, A_STUDENT, **extra).status_code == 422
    with session_factory() as s:
        assert s.execute(select(AgentMission)).scalars().first() is None  # nothing was created


def test_another_tenant_cannot_reach_the_mission(client) -> None:
    mid = ask(client, A_STUDENT).json()["mission_id"]
    for email in (B_STUDENT, B_ADMIN):
        headers = bearer(login(client, email)["access_token"])
        assert client.get(f"/agentos/missions/{mid}", headers=headers).status_code == 404
        assert client.get(f"/agentos/missions/{mid}/steps", headers=headers).status_code == 404
        assert client.post(f"/agentos/missions/{mid}/run-step", headers=headers).status_code == 404
        assert all(m["mission_id"] != mid for m in client.get("/agentos/assistant/missions", headers=headers).json())


def test_assistant_mission_listing_is_scoped_to_the_caller(client) -> None:
    student_missions = {ask(client, A_STUDENT).json()["mission_id"] for _ in range(2)}
    faculty_mission = ask(client, A_FACULTY).json()["mission_id"]
    listed = lambda email: [m["mission_id"] for m in client.get(  # noqa: E731
        "/agentos/assistant/missions", headers=bearer(login(client, email)["access_token"])).json()]
    assert set(listed(A_STUDENT)) == student_missions and listed(A_STUDENT) == sorted(student_missions, reverse=True)
    assert listed(A_FACULTY) == [faculty_mission]
    assert listed(B_STUDENT) == []
    assert client.get("/agentos/assistant/missions").status_code == 401


# --- The Groq brain ---------------------------------------------------------------------------------------------------


def test_a_valid_groq_decision_runs_through_the_kernel(app, client, session_factory) -> None:
    fake = use_groq(app, [IDENTITY, DONE])
    body = ask(client, A_STUDENT).json()
    assert (body["status"], body["assistant_message"], body["steps_performed"]) == ("completed", DONE["user_message"], 2)
    assert body["brain"] == {"provider": "groq", "model": "openai/gpt-oss-20b", "live": True}
    request = fake.calls[0]
    assert set(request) == {"system", "content", "schema_name", "schema", "max_tokens", "operation"}  # no provider tools
    schema = request["schema"]
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["kind"]["enum"] == ["tool", "ask_human", "complete", "fail"]  # nothing delegable yet
    assert schema["properties"]["tool_name"]["enum"] == ["consult_domain_specialist", "get_assignment_status", "get_my_active_missions",
                                                         "get_my_assignments", "get_my_attendance_summary",
                                                         "get_my_exams", "get_my_identity_context", None]
    _, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert steps[0].tool_name == "get_my_identity_context" and steps[1].input_summary["user_message"] == DONE["user_message"]


def test_the_groq_request_is_strict_json_schema_without_tools_or_reasoning() -> None:
    bodies = []

    class Client:
        def post(self, path, json):
            bodies.append(json)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"kind": "fail"}'}}],
                                             "usage": {"prompt_tokens": 50, "completion_tokens": 9}})

    provider = GroqLLMProvider(model="openai/gpt-oss-20b", api_key="gsk_test", client=Client(), sleep=lambda _s: None)
    out = provider.complete_json_schema(system="s", content="c", schema_name="agent_decision", schema={"type": "object"},
                                        max_tokens=64, operation="agent_brain_decide")
    assert out == {"kind": "fail"}
    body = bodies[0]
    assert "tools" not in body and "tool_choice" not in body
    assert body["response_format"]["json_schema"]["strict"] is True and body["include_reasoning"] is False
    assert provider.take_last_usage() == (50, 9)


@pytest.mark.parametrize("raw,code", [
    ({"kind": "complete", "outcome": "x", "user_message": "y", "reasoning": "I think..."}, "UNEXPECTED_FIELDS"),
    (wire("tool", tool_name="get_my_identity_context", tool_input_json="{not json"), "MALFORMED_TOOL_INPUT"),
    (wire("wait"), "KIND_NOT_OFFERED"),
    (wire("complete", outcome="x", reason="also a reason"), "INVALID_DECISION_SHAPE"),
    (["not", "an", "object"], "MALFORMED_OUTPUT"),
])
def test_parse_decision_refuses_anything_but_a_clean_decision(raw, code) -> None:
    context = _context(tools=True)
    with pytest.raises(BrainOutputError) as exc:
        parse_decision(raw, decision_schema(context))
    assert exc.value.code == code


def test_model_written_text_never_carries_a_url() -> None:
    raw = wire("complete", outcome="see https://evil.example/x", user_message="Log in at www.evil.example or ftp://x.y now")
    decision = parse_decision(raw, decision_schema(_context(tools=True)))
    assert "evil" not in decision.user_message + decision.outcome and "[link removed]" in decision.user_message


@pytest.mark.parametrize("answer,cause", [
    ({**DONE, "reasoning": "secret chain of thought"}, "UNEXPECTED_FIELDS"),
    (None, "MALFORMED_OUTPUT"),  # real provider, content is not JSON
])
def test_malformed_or_reasoning_output_is_rejected_and_nothing_runs(app, client, session_factory, answer, cause) -> None:
    if answer is None:
        class Client:
            def post(self, path, json):
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "Sure! {kind:"}}]})
        provider = GroqLLMProvider(model="openai/gpt-oss-20b", api_key="gsk_test", client=Client(), sleep=lambda _s: None)
        use_brain(app, GroqAgentBrain(provider))
    else:
        use_groq(app, [answer])
    body = ask(client, A_STUDENT).json()
    assert (body["status"], body["steps_performed"], body["assistant_message"]) == ("failed", 1, None)
    mission, steps, audits = mission_rows(session_factory, body["mission_id"])
    assert steps[0].status == AgentStepStatus.REJECTED and steps[0].tool_name is None
    assert steps[0].input_summary == {"kind": "invalid", "cause": cause}
    assert "chain of thought" not in json.dumps([mission.context, steps[0].output_summary, [a.event_metadata for a in audits]])
    assert "AI_BRAIN_FAILED" in [a.event_type for a in audits]


def test_an_unknown_tool_is_rejected_by_the_kernel(app, client, session_factory) -> None:
    use_groq(app, [wire("tool", tool_name="drop_all_tables", tool_input_json="{}")])
    body = ask(client, A_STUDENT).json()
    _, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert body["status"] == "failed" and steps[0].status == AgentStepStatus.REJECTED
    assert steps[0].output_summary == {"error_code": "UNAUTHORIZED_TOOL"}


def test_a_provider_timeout_leaves_the_mission_resumable(app, client, session_factory) -> None:
    class Client:
        def post(self, path, json):
            raise httpx.ReadTimeout("timed out")
    provider = GroqLLMProvider(model="openai/gpt-oss-20b", api_key="gsk_test", client=Client(), sleep=lambda _s: None,
                               max_retries=1)
    use_brain(app, GroqAgentBrain(provider, recorder=app.state.llm_provider.recorder))
    refused = ask(client, A_STUDENT)
    assert refused.status_code == 503
    detail = refused.json()["detail"]
    assert (detail["code"], detail["status"], detail["steps_performed"]) == ("PROVIDER_TIMEOUT", "pending", 0)
    mission, steps, audits = mission_rows(session_factory, detail["mission_id"])
    assert (mission.status, steps) == (AgentMissionStatus.PENDING, [])
    assert [a.event_type for a in audits][-2:] == ["AI_BRAIN_CALLED", "AI_BRAIN_FAILED"]

    use_groq(app, [DONE])  # the provider is back: the same mission continues
    headers = bearer(login(client, A_STUDENT)["access_token"])
    resumed = client.post(f"/agentos/missions/{detail['mission_id']}/run-step", headers=headers).json()
    assert resumed["status"] == "completed"


def test_provider_failures_never_fall_back_to_mock(app, client, session_factory, monkeypatch) -> None:
    use_groq(app, [LLMProviderError("auth failed for gsk_live_key"), LLMTransientError("429", provider="groq", model="m")])
    first = ask(client, A_STUDENT)
    assert first.status_code == 503 and first.json()["detail"]["code"] == "PROVIDER_ERROR"
    assert "mock" not in json.dumps(first.json()).lower()

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    brain = build_agent_brain("groq")
    assert isinstance(brain, UnavailableBrain) and brain.code == "PROVIDER_NOT_CONFIGURED"
    assert build_agent_brain("anthropic").code == "AGENT_BRAIN_NOT_SUPPORTED"
    use_brain(app, brain)
    with session_factory() as s:
        before = len(s.execute(select(AgentMission)).scalars().all())
    refused = ask(client, A_STUDENT)
    assert refused.status_code == 503 and refused.json()["detail"]["code"] == "PROVIDER_NOT_CONFIGURED"
    with session_factory() as s:
        assert len(s.execute(select(AgentMission)).scalars().all()) == before  # refused before creating a mission


def test_an_exhausted_ai_budget_stops_the_brain_explicitly(app, client, session_factory, orgs) -> None:
    fake = use_groq(app, [DONE])
    with session_factory() as s:
        s.get(Organization, orgs["a"]).monthly_ai_budget_usd = 0.0
        s.commit()
    refused = ask(client, A_STUDENT)
    assert refused.status_code == 402 and refused.json()["detail"]["code"] == "AI_BUDGET_EXCEEDED"
    assert fake.calls == []


def test_nexus_can_complete_directly_with_a_user_message(app, client) -> None:
    use_groq(app, [DONE])
    body = ask(client, A_STUDENT, message="Book me a hostel room").json()
    assert (body["status"], body["assistant_message"], body["steps_performed"]) == ("completed", DONE["user_message"], 1)


# --- Scope and secrets -------------------------------------------------------------------------------------------------


def test_a_student_cannot_ask_for_another_students_context(app, client, session_factory, orgs) -> None:
    use_groq(app, [wire("tool", tool_name="get_my_identity_context", tool_input_json='{"student_code": "STU-B-001"}')])
    body = ask(client, A_STUDENT, message="Show me STU-B-001's profile").json()
    _, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert body["status"] == "failed" and steps[0].output_summary == {"error_code": "INVALID_TOOL_INPUT"}

    use_groq(app, [IDENTITY, DONE])
    body = ask(client, A_STUDENT).json()
    _, steps, _ = mission_rows(session_factory, body["mission_id"])
    with session_factory() as s:
        own = s.execute(select(OrganizationMembership.student_id).join(AuthAccount, AuthAccount.id == OrganizationMembership.account_id)
                        .where(AuthAccount.email == A_STUDENT)).scalar_one()
        own_code = s.get(Student, own).student_code
    assert steps[0].output_summary["data"]["student"]["student_code"] == own_code


def test_faculty_identity_context_never_contains_phone_numbers_or_secrets(app, client, session_factory) -> None:
    with session_factory() as s:
        profile_id = s.execute(select(OrganizationMembership.faculty_profile_id).join(
            AuthAccount, AuthAccount.id == OrganizationMembership.account_id).where(AuthAccount.email == A_FACULTY)).scalar_one()
        s.get(FacultyProfile, profile_id).phone = PHONE
        s.commit()
    fake = use_groq(app, [IDENTITY, DONE])
    body = ask(client, A_FACULTY, message=f"My password is {SECRET}, call me on {PHONE}").json()
    mission, steps, audits = mission_rows(session_factory, body["mission_id"])
    stored = json.dumps([mission.goal, mission.context, [(s.input_summary, s.output_summary) for s in steps],
                         [(a.message, a.event_metadata) for a in audits]])
    sent = json.dumps([c["content"] for c in fake.calls])
    for text in (stored, sent):
        assert "98765" not in text and "phone" not in text.lower()
    assert "phone" not in steps[0].output_summary["data"].get("faculty", {})
    assert GetMyIdentityContext.input_model.model_json_schema()["properties"] == {}


def test_telemetry_and_audit_hold_no_prompt_output_or_secrets(app, client, session_factory, monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_live_should_never_appear")
    use_groq(app, [IDENTITY, DONE])
    body = ask(client, A_STUDENT, message=f"Remember {SECRET} for me").json()
    _, _, audits = mission_rows(session_factory, body["mission_id"])
    with session_factory() as s:
        usage = list(s.execute(select(AIUsageEvent).where(AIUsageEvent.mission_id == f"agentos:{body['mission_id']}")).scalars())
        s.expunge_all()
    assert [(u.operation, u.provider, u.model, u.agent_key, u.success, u.input_tokens, u.output_tokens) for u in usage] == [
        ("agent_brain_decide", "groq", "openai/gpt-oss-20b", NEXUS_AGENT_KEY, True, 120, 30)] * 2
    assert all(u.estimated_cost_usd is not None and u.latency_ms is not None for u in usage)
    brain_audits = [a for a in audits if a.event_type.startswith("AI_BRAIN_")]
    assert {k for a in brain_audits for k in a.event_metadata} <= {"agent_key", "provider", "model", "latency_ms", "error_code"}
    dumped = json.dumps([[(a.message, a.event_metadata) for a in audits],
                         [{c.name: getattr(u, c.name) for c in AIUsageEvent.__table__.columns} for u in usage]], default=str)
    for leaked in (SECRET, "gsk_live", SYSTEM_PROMPT[:40], DONE["user_message"], "tool_input_json"):
        assert leaked not in dumped


# --- Bounded context ---------------------------------------------------------------------------------------------------


def _context(*, tools: bool, observations: int = 0, goal: str = "Who am I?") -> AgentContext:
    now = datetime.now(timezone.utc)
    return AgentContext(
        mission_id=1, agent_key=NEXUS_AGENT_KEY, goal=goal, success_criteria=["reply"], status=AgentMissionStatus.RUNNING,
        step_count=observations, max_steps=8, caller_role="student",
        state={"inputs": {"channel": "assistant"}, "failure": {"code": "X"}},
        observations=[AgentObservation(kind="tool_result", source="get_my_identity_context", at=now,
                                       data={"n": i, "note": "x" * 5000, "contact": f"call {PHONE}"}) for i in range(observations)],
        allowed_tools=[ToolDescriptor(name="get_my_identity_context", description="me", input_schema={"properties": {}})]
        if tools else [],
    )


def test_brain_context_is_bounded_redacted_and_minimal() -> None:
    payload = brain_payload(_context(tools=True, observations=20, goal="g" * 5000), max_history=8)
    text = json.dumps(payload)
    observations = payload["untrusted"]["observations"]
    assert len(text) <= MAX_PROMPT_CHARS and 1 <= len(observations) <= 8
    assert observations[-1]["data"]["n"] == 19  # the newest are kept
    assert "98765" not in text and len(payload["untrusted"]["request"]) < 1100
    assert set(payload) == {"agent", "caller_role", "progress", "decision_kinds", "tools", "delegate_agents", "untrusted"}
    assert "failure" not in text and "inputs" not in text  # unrelated mission state is not sent
    assert brain_payload(_context(tools=False, observations=20), max_history=50)["untrusted"]["observations"].__len__() <= 10


def test_runtime_working_memory_is_capped(app, client, session_factory) -> None:
    use_groq(app, [wire("tool", tool_name="get_my_active_missions", tool_input_json='{"limit": 2}')] * 4)
    body = ask(client, A_STUDENT).json()
    mission, _, _ = mission_rows(session_factory, body["mission_id"])
    assert body["status"] == "running" and body["steps_performed"] == 4  # bounded transitions per request
    assert len(mission.context["observations"]) <= RECENT_OBSERVATIONS == 8
    many = AgentMission(context={"observations": []})
    AgentRuntime._remember(many, list(_context(tools=True, observations=12).observations))
    assert [o["data"]["n"] for o in many.context["observations"]] == list(range(4, 12))
    assert DecisionKind.TOOL.value in decision_schema(_context(tools=True))["properties"]["kind"]["enum"]
