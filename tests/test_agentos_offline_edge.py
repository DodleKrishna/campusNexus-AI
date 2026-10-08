"""AgentOS V2 Phase 6: offline edge intelligence and local Nexus voice.

No real model, binary or network: Ollama is a fake HTTP client (whose answers come from the deterministic mock
policies, so the same Guardian flows run through the *local* brain path), whisper.cpp / Piper are fake subprocess
runners, and connectivity is a switch. ``httpx.Client.send`` is patched to fail in the offline tests, so any cloud
call would surface.
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import wave
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agentos.brain import BrainOutputError, BrainUnavailableError
from app.agentos.brain_router import ConnectivityAwareBrainRouter, build_configured_brain
from app.agentos.connectivity import ConnectivityService, ConnectivityState, publish_transition
from app.agentos.local_brain import (
    LocalEndpointError, OllamaAgentBrain, OllamaConfig, OllamaHTTP, chat_request, validate_local_endpoint,
)
from app.agentos.nexus import UNSTORED_GOAL
from app.agentos.providers import GroqAgentBrain, MockAgentBrain, UnavailableBrain, decision_schema
from app.agentos.schemas import AgentContext, AgentDecision, DecisionKind, ToolDescriptor
from app.communication.connectors import ConnectorRegistry
from app.communication.voice.audio import SAMPLE_RATE, pcm_to_wav, wav_to_pcm
from app.communication.voice.exotel import ExotelVoiceConnector
from app.communication.voice.local_speech import (
    LocalToolError, LocalWhisperSTTProvider, PiperConfig, PiperTTSProvider, WhisperConfig, piper_argv,
    resample_linear16, trusted_file, whisper_argv,
)
from app.communication.voice.speech import SpeechProviderError, Transcript
from app.communication.voice.speech_router import MeteredSTT, MeteredTTS, SpeechRouter
from app.communication.worker import RELEASE_SPACING, WAITING_CONNECTIVITY, process_communication_jobs
from app.db.models import (
    AgentMission, AgentMissionStatus, AgentStep, CommunicationJob, CommunicationJobStatus, CommunicationPreference,
    ContactKind, ContactPoint, DomainEvent, FollowupStatus, OperationAuditEvent,
)
from app.db.models.ai_usage import AIUsageEvent
from app.llm.base import LLMTransientError
from app.schemas.enums import UserRole
from tests.test_agentos_assignment_guardian import (  # noqa: F401 -- fixtures and helpers
    T0, clock, mission_of, published, rows, run_worker, small_class, submit,
)
from tests.test_agentos_communication import advance, comm, jobs, notifications, ready_job, requested, tenant
from tests.test_agentos_communication_voice import CONFIG, PHONE, VOICE_POLICY, FakeExotel
from tests.test_agentos_exam_attendance_guardians import (  # noqa: F401
    ATT_POLICY, CLASS_START, EXAM_POLICY, START, absent_class, exam_followups, interventions, mark, scheduled_exam,
    use_brain as use_guardian_brain,
)
from tests.test_agentos_nexus import IDENTITY, FakeGroq, account_id, ask, mission_rows, use_brain, wire
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_STUDENT, B_STUDENT, app, bearer, client, login, orgs,
)

THINKING = "PRIVATE-CHAIN-OF-THOUGHT-should-never-be-stored"
SPOKEN = "Who am I and what assignments are pending?"


# --- Fakes ---------------------------------------------------------------------------------------------------------


class Net:
    """A connectivity switch with the ConnectivityService surface the code uses."""

    def __init__(self, online: bool) -> None:
        self.online, self.checks, self.invalidations = online, 0, 0

    def state(self) -> ConnectivityState:
        self.checks += 1
        return ConnectivityState.CLOUD_REACHABLE if self.online else ConnectivityState.LOCAL_ONLY

    def invalidate(self) -> None:
        self.invalidations += 1

    def label(self) -> str:
        return "online" if self.online else "offline"


class FakeResponse:
    def __init__(self, status_code: int, body) -> None:
        self.status_code, self._body = status_code, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def to_wire(decision: AgentDecision, schema: dict) -> dict:
    """An AgentDecision in the strict wire shape (every schema field, unused ones null)."""
    values = {name: None for name in schema["properties"]}
    values["kind"] = decision.kind.value
    if decision.kind == DecisionKind.TOOL:
        values.update(tool_name=decision.tool_name, tool_input_json=json.dumps(decision.tool_input or {}))
    for name in ("question", "outcome", "reason", "user_message", "wake_after_seconds", "plan"):
        if getattr(decision, name, None) is not None and name in values:
            values[name] = getattr(decision, name)
    if decision.wait_for is not None:
        values["wait_for"] = getattr(decision.wait_for, "value", decision.wait_for)
    return values


def mock_policy(payload: dict) -> AgentDecision:
    """The deterministic Guardian/Communication mock policies, driven only by what the local model is sent."""
    context = SimpleNamespace(
        agent_key=payload["agent"], caller_role=payload["caller_role"],
        allowed_tools=[SimpleNamespace(name=t["name"]) for t in payload["tools"]],
        observations=[SimpleNamespace(**o) for o in payload["untrusted"]["observations"]],
        state={"supervisor": payload.get("facts") or {}})
    return MockAgentBrain._decide(context)


def nexus_policy(payload: dict) -> AgentDecision:
    """'Who am I and what assignments are pending?': identity, then assignments, then a reply from the results."""
    done = {o["source"]: o["data"].get("data") or {} for o in payload["untrusted"]["observations"]
            if o["kind"] == "tool_result"}
    request = payload["untrusted"]["request"].lower()
    if "get_my_identity_context" not in done:
        return AgentDecision(kind=DecisionKind.TOOL, tool_name="get_my_identity_context")
    if "assignment" in request and "get_my_assignments" not in done:
        return AgentDecision(kind=DecisionKind.TOOL, tool_name="get_my_assignments")
    pending = [a for a in (done.get("get_my_assignments") or {}).get("assignments", []) if not a.get("my_submission")]
    who = done["get_my_identity_context"].get("display_name") or "you"
    return AgentDecision(kind=DecisionKind.COMPLETE, outcome="Answered locally.",
                         user_message=f"You are {who}. Pending assignments: {len(pending)}.")


class FakeOllama:
    """An Ollama /api/chat + /api/tags stand-in. ``policy(payload) -> AgentDecision`` or a raw reply override."""

    def __init__(self, policy=None, *, models=("gpt-oss:20b",), replies=None) -> None:
        self.policy, self.models, self.replies = policy or mock_policy, list(models), list(replies or [])
        self.requests: list = []

    def get(self, path):
        assert path == "/api/tags"
        return FakeResponse(200, {"models": [{"name": m} for m in self.models]})

    def post(self, path, json=None):
        assert path == "/api/chat"
        self.requests.append(json)
        if json["model"] not in self.models:
            return FakeResponse(404, {"error": "model not found"})
        if self.replies:
            reply = self.replies.pop(0)
            return reply if isinstance(reply, FakeResponse) else FakeResponse(200, reply)
        payload = __import__("json").loads(json["messages"][1]["content"])
        decision = self.policy(payload)
        content = __import__("json").dumps(to_wire(decision, json["format"]))
        return FakeResponse(200, {"model": json["model"], "done": True, "done_reason": "stop",
                                  "message": {"role": "assistant", "content": content, "thinking": THINKING},
                                  "prompt_eval_count": 321, "eval_count": 45})


def ollama_brain(fake: FakeOllama, *, recorder=None, **config) -> OllamaAgentBrain:
    cfg = OllamaConfig(**config)
    return OllamaAgentBrain(cfg, http=OllamaHTTP(cfg, client=fake), recorder=recorder)


class CountingBrain:
    """A cloud brain that only counts (and optionally fails)."""

    provider_name, model_name, is_live, available, audit_calls = "groq", "openai/gpt-oss-20b", True, True, True

    def __init__(self, error=None) -> None:
        self.calls, self.error = 0, error

    def decide(self, context):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AgentDecision(kind=DecisionKind.COMPLETE, outcome="cloud", user_message="cloud")


def local_router(app, fake: FakeOllama, *, mode="local", cloud=None, net=None):
    net = net or Net(online=False)
    brain = ollama_brain(fake, recorder=app.state.llm_provider.recorder)
    return ConnectivityAwareBrainRouter(mode, connectivity=net, cloud=cloud, local=brain), net


@pytest.fixture()
def no_network(monkeypatch):
    """Any real HTTP request (a cloud provider) fails the test."""
    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("a network request was attempted")

    monkeypatch.setattr(httpx.Client, "send", refuse)


def context(goal="Who am I?", tools=("get_my_identity_context",)) -> AgentContext:
    from app.db.models.agent_kernel import AgentMissionStatus as S
    return AgentContext(mission_id=1, agent_key="nexus_orchestrator", goal=goal, success_criteria=[], status=S.RUNNING,
                        step_count=0, max_steps=8, caller_role="student",
                        allowed_tools=[ToolDescriptor(name=t, description="d", input_schema={}) for t in tools])


def db_text(session_factory) -> str:
    """Every row of every table, as text (to prove something was never stored)."""
    from app.db.base import Base

    out = []
    with session_factory() as s:
        for table in Base.metadata.sorted_tables:
            for row in s.execute(select(table)).all():
                out.append(repr(tuple(row)))
    return "\n".join(out)


# --- 1-5: routing ----------------------------------------------------------------------------------------------------


def test_local_mode_invokes_only_the_local_brain() -> None:
    cloud, fake = CountingBrain(), FakeOllama(nexus_policy)
    router = ConnectivityAwareBrainRouter("local", connectivity=Net(True), cloud=cloud, local=ollama_brain(fake))
    decision = router.decide(context())
    assert decision.tool_name == "get_my_identity_context" and len(fake.requests) == 1 and cloud.calls == 0
    assert (router.provider_name, router.model_name, router.last_route) == ("ollama", "gpt-oss:20b", "local_mode")
    assert router.cloud is None  # local mode does not even hold a cloud brain


def test_cloud_mode_never_calls_ollama_even_offline() -> None:
    fake = FakeOllama(nexus_policy)
    for error in (None, BrainUnavailableError("down", code="PROVIDER_UNAVAILABLE")):
        cloud = CountingBrain(error)
        router = ConnectivityAwareBrainRouter("cloud", connectivity=Net(False), cloud=cloud, local=ollama_brain(fake))
        if error is None:
            assert router.decide(context()).outcome == "cloud"
        else:
            with pytest.raises(BrainUnavailableError) as exc:
                router.decide(context())
            assert exc.value.code == "PROVIDER_UNAVAILABLE"  # fails explicitly
        assert cloud.calls == 1
    assert fake.requests == []


def test_auto_mode_uses_local_on_connectivity_loss_and_cloud_when_online() -> None:
    cloud, fake, net = CountingBrain(), FakeOllama(nexus_policy), Net(False)
    router = ConnectivityAwareBrainRouter("auto", connectivity=net, cloud=cloud, local=ollama_brain(fake))
    assert router.decide(context()).tool_name == "get_my_identity_context"
    assert (cloud.calls, len(fake.requests), router.last_route) == (0, 1, "connectivity_lost")
    net.online = True
    assert router.decide(context()).outcome == "cloud" and router.last_route == "cloud_preferred"
    assert (cloud.calls, len(fake.requests), router.provider_name) == (1, 1, "groq")


def test_auto_mode_moves_to_local_only_on_a_cloud_transport_failure() -> None:
    groq = FakeGroq([LLMTransientError("boom", provider="groq", model="m", kind="network")])
    fake, net = FakeOllama(nexus_policy), Net(True)
    router = ConnectivityAwareBrainRouter("auto", connectivity=net, cloud=GroqAgentBrain(groq), local=ollama_brain(fake))
    assert router.decide(context()).tool_name == "get_my_identity_context"
    assert (len(groq.calls), len(fake.requests), router.last_route, net.invalidations) == (
        1, 1, "cloud_transport_failed", 1)


@pytest.mark.parametrize("error, code", [
    (BrainUnavailableError("budget", code="AI_BUDGET_EXCEEDED"), "AI_BUDGET_EXCEEDED"),
    (BrainUnavailableError("429", code="PROVIDER_RATE_LIMITED"), "PROVIDER_RATE_LIMITED"),
    (BrainUnavailableError("auth", code="PROVIDER_ERROR"), "PROVIDER_ERROR"),
])
def test_budget_rate_limit_or_auth_failures_never_switch_to_local(error, code) -> None:
    cloud, fake = CountingBrain(error), FakeOllama(nexus_policy)
    router = ConnectivityAwareBrainRouter("auto", connectivity=Net(True), cloud=cloud, local=ollama_brain(fake))
    with pytest.raises(BrainUnavailableError) as exc:
        router.decide(context())
    assert exc.value.code == code and fake.requests == []


@pytest.mark.parametrize("answer", [{"kind": "complete"}, {**wire("complete", outcome="x"), "reasoning": "hidden"}])
def test_malformed_cloud_output_never_switches_to_local(answer) -> None:
    fake = FakeOllama(nexus_policy)
    router = ConnectivityAwareBrainRouter("auto", connectivity=Net(True), cloud=GroqAgentBrain(FakeGroq([answer])),
                                          local=ollama_brain(fake))
    with pytest.raises(BrainOutputError):
        router.decide(context())
    assert fake.requests == []


def test_offline_mode_refuses_every_cloud_provider(monkeypatch) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_OFFLINE_MODE", "1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_not_real")
    from app.agentos.providers import build_agent_brain
    from app.communication.voice.conversation import voice_pipeline_from_env

    monkeypatch.setenv("CAMPUSNEXUS_TTS_VOICE", "test-voice")
    assert build_agent_brain("groq").code == "CLOUD_DISABLED_OFFLINE"
    assert voice_pipeline_from_env() == (None, "CLOUD_DISABLED_OFFLINE")
    monkeypatch.setenv("CAMPUSNEXUS_INTELLIGENCE_MODE", "cloud")
    router = build_configured_brain()
    assert not router.available and router.code == "CLOUD_DISABLED_OFFLINE"
    assert ConnectivityService.from_env().state() == ConnectivityState.LOCAL_ONLY
    cloud, fake = CountingBrain(), FakeOllama(nexus_policy)
    auto = ConnectivityAwareBrainRouter("auto", connectivity=Net(True), cloud=cloud, local=ollama_brain(fake))
    auto.decide(context())
    assert (cloud.calls, len(fake.requests), auto.last_route) == (0, 1, "offline_mode")


def test_unset_mode_keeps_the_phase2_brain_and_invalid_mode_refuses(monkeypatch) -> None:
    assert isinstance(build_configured_brain(), MockAgentBrain)
    monkeypatch.setenv("CAMPUSNEXUS_INTELLIGENCE_MODE", "turbo")
    assert build_configured_brain().code == "INVALID_INTELLIGENCE_MODE"
    monkeypatch.setenv("CAMPUSNEXUS_INTELLIGENCE_MODE", "auto")
    monkeypatch.setenv("CAMPUSNEXUS_AGENT_BRAIN", "mock")  # the mock is never a "cloud" brain
    router = build_configured_brain(connectivity=Net(True))
    assert router.cloud.code == "CLOUD_BRAIN_NOT_SUPPORTED"


# --- 6-8: the local brain ------------------------------------------------------------------------------------------


def test_local_brain_request_is_strict_schema_without_tools_and_validated() -> None:
    fake = FakeOllama(nexus_policy)
    decision = ollama_brain(fake).decide(context())
    assert isinstance(decision, AgentDecision) and decision.tool_name == "get_my_identity_context"
    request = fake.requests[0]
    assert set(request) == {"model", "stream", "format", "messages", "options"}  # no "tools", no pull, no stream
    assert request["stream"] is False and request["format"] == decision_schema(context())
    assert request["format"]["additionalProperties"] is False


@pytest.mark.parametrize("message, done_reason, code", [
    ({"content": json.dumps({**wire("complete", outcome="x", user_message="y"), "reasoning": "hidden"})}, "stop",
     "UNEXPECTED_FIELDS"),
    ({"content": "not json"}, "stop", "MALFORMED_OUTPUT"),
    ({"content": json.dumps(wire("tool", tool_input_json="{}"))}, "stop", "INVALID_DECISION_SHAPE"),
    ({"content": json.dumps(wire("complete", outcome="x")), "tool_calls": [{"function": {"name": "x"}}]}, "stop",
     "UNEXPECTED_TOOL_CALL"),
    ({"content": json.dumps(wire("complete", outcome="x"))}, "length", "TRUNCATED_OUTPUT"),
])
def test_local_brain_rejects_anything_but_a_clean_decision(message, done_reason, code) -> None:
    fake = FakeOllama(replies=[{"message": message, "done_reason": done_reason}])
    with pytest.raises(BrainOutputError) as exc:
        ollama_brain(fake).decide(context())
    assert exc.value.code in (code, "KIND_NOT_OFFERED")


def test_local_model_unavailable_and_explicit_fallback_only() -> None:
    fake = FakeOllama(nexus_policy, models=("qwen3:4b",))
    with pytest.raises(BrainUnavailableError) as exc:
        ollama_brain(fake).decide(context())
    assert exc.value.code == "LOCAL_MODEL_UNAVAILABLE"
    configured_not_enabled = ollama_brain(fake, fallback_model="qwen3:4b", fallback_enabled=False)
    with pytest.raises(BrainUnavailableError):
        configured_not_enabled.decide(context())
    brain = ollama_brain(fake, fallback_model="qwen3:4b", fallback_enabled=True)
    assert brain.decide(context()).tool_name == "get_my_identity_context" and brain.model_name == "qwen3:4b"
    # A bad answer from an installed model is not "unavailable": no fallback.
    bad = FakeOllama(models=("gpt-oss:20b", "qwen3:4b"), replies=[{"message": {"content": "{"}, "done_reason": "stop"}])
    with pytest.raises(BrainOutputError):
        ollama_brain(bad, fallback_model="qwen3:4b", fallback_enabled=True).decide(context())
    assert [r["model"] for r in bad.requests] == ["gpt-oss:20b"]


def test_ollama_config_from_env(monkeypatch) -> None:
    for key in (
        "CAMPUSNEXUS_OLLAMA_BASE_URL",
        "CAMPUSNEXUS_OLLAMA_MODEL",
        "CAMPUSNEXUS_OLLAMA_FALLBACK_MODEL",
        "CAMPUSNEXUS_OLLAMA_FALLBACK_POLICY",
        "CAMPUSNEXUS_OLLAMA_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)

    config = OllamaConfig.from_env()
    assert (config.base_url, config.model, config.fallback_enabled) == (
        "http://127.0.0.1:11434",
        "gpt-oss:20b",
        False,
    )


@pytest.mark.parametrize("url, allowed", [
    ("http://127.0.0.1:11434", True), ("http://localhost:11434", True), ("http://[::1]:11434", True),
    ("http://10.0.0.5:11434", True), ("http://192.168.1.20:11434/", True),
    ("http://evil.example.com:11434", False), ("http://8.8.8.8:11434", False),
    ("http://169.254.169.254", False), ("http://0.0.0.0:11434", False), ("file:///etc/passwd", False),
    ("http://user:pass@127.0.0.1:11434", False), ("http://127.0.0.1:11434/api/../x", False),
    ("http://127.0.0.1:11434?redirect=http://evil", False), ("gopher://127.0.0.1", False),
])
def test_arbitrary_ollama_urls_are_rejected(url, allowed) -> None:
    if allowed:
        assert validate_local_endpoint(url).startswith("http")
    else:
        with pytest.raises(LocalEndpointError):
            validate_local_endpoint(url)


def test_remote_ollama_needs_an_explicit_operator_opt_in_and_never_metadata() -> None:
    assert validate_local_endpoint("http://ollama.internal:11434", allow_remote=True) == "http://ollama.internal:11434"
    with pytest.raises(LocalEndpointError):
        validate_local_endpoint("http://169.254.169.254", allow_remote=True)


def test_ollama_http_client_ignores_proxies_and_redirects() -> None:
    http = OllamaHTTP(OllamaConfig())
    assert http._client.trust_env is False and http._client.follow_redirects is False
    assert http._client.timeout.read == OllamaConfig().timeout
    http._client.close()


# --- 7, 9, 10: offline Nexus through the API -----------------------------------------------------------------------


def test_offline_nexus_runs_the_normal_tools_with_zero_cloud_calls(app, client, session_factory, no_network) -> None:
    cloud, fake = CountingBrain(), FakeOllama(nexus_policy)
    router, net = local_router(app, fake, mode="auto", cloud=cloud)
    use_brain(app, router)
    response = ask(client, A_STUDENT, SPOKEN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed" and body["brain"] == {"provider": "ollama", "model": "gpt-oss:20b", "live": True}
    assert body["assistant_message"].startswith("You are ") and "Pending assignments: 0" in body["assistant_message"]
    assert cloud.calls == 0 and len(fake.requests) == 3
    mission, steps, audits = mission_rows(session_factory, body["mission_id"])
    assert mission.owner_account_id == account_id(session_factory, A_STUDENT)
    assert [(s.action_type, s.tool_name) for s in steps] == [
        ("tool", "get_my_identity_context"), ("tool", "get_my_assignments"), ("complete", None)]
    brain_audits = [a for a in audits if a.event_type == "AI_BRAIN_CALLED"]
    assert [(a.event_metadata["provider"], a.event_metadata["route"]) for a in brain_audits] == [
        ("ollama", "connectivity_lost")] * 3
    assert "MISSION_COMPLETED" in [a.event_type for a in audits]
    usage = rows(session_factory, AIUsageEvent, AIUsageEvent.mission_id == f"agentos:{body['mission_id']}")
    assert {(u.provider, u.model, u.success) for u in usage} == {("ollama", "gpt-oss:20b", True)}
    assert all(u.estimated_cost_usd is None and u.input_tokens == 321 and u.output_tokens == 45 for u in usage)
    assert THINKING not in db_text(session_factory)  # Ollama's thinking field is never kept


def test_local_brain_bad_answer_is_rejected_by_the_kernel(app, client, session_factory) -> None:
    content = json.dumps({**wire("complete", outcome="x", user_message="y"), "reasoning": THINKING})
    fake = FakeOllama(replies=[{"message": {"content": content, "thinking": THINKING}, "done_reason": "stop"}])
    use_brain(app, local_router(app, fake)[0])
    body = ask(client, A_STUDENT).json()
    mission, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert mission.status == AgentMissionStatus.FAILED and steps[-1].status.value == "rejected"
    assert THINKING not in db_text(session_factory)


def test_local_model_missing_refuses_explicitly_and_keeps_the_mission(app, client, session_factory) -> None:
    use_brain(app, local_router(app, FakeOllama(models=()))[0])
    response = ask(client, A_STUDENT)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "LOCAL_MODEL_UNAVAILABLE"
    mission, steps, _ = mission_rows(session_factory, response.json()["detail"]["mission_id"])
    assert steps == [] and mission.status != AgentMissionStatus.FAILED  # resumable, nothing executed


# --- 11-13: the Guardians on the local brain -----------------------------------------------------------------------


def guardians_on(app, fake: FakeOllama):
    router, _ = local_router(app, fake)
    runtime = use_guardian_brain(app, router)
    return runtime


def test_assignment_guardian_runs_on_the_local_brain(client, small_class, app, clock, session_factory, no_network) -> None:
    fake = FakeOllama()
    guardians_on(app, fake)
    result = published(client, small_class)
    aid, mid = result["assignment"]["id"], result["guardian_mission_id"]
    clock.advance(minutes=1)
    submit(client, "GRD-001", aid)
    clock.advance(minutes=1)
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert [(s.action_type, s.tool_name) for s in steps] == [
        ("tool", "get_pending_students"), ("tool", "request_student_followup"), ("wait", None)]
    assert len(fake.requests) == 3
    for code in ("GRD-002", "GRD-003"):
        clock.advance(minutes=5)
        submit(client, code, aid)
    clock.advance(minutes=1)
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert mission.status == AgentMissionStatus.COMPLETED and steps[-1].action_type == "verified_complete"
    assert len(fake.requests) == 3  # the deterministic supervisor completed it: no further model call


def test_exam_guardian_runs_on_the_local_brain(client, small_class, app, clock, session_factory, no_network) -> None:
    fake = FakeOllama()
    guardians_on(app, fake)
    from tests.test_agentos_exam_attendance_guardians import _start_and_mark
    result = scheduled_exam(client, small_class)
    eid, mid = result["exam"]["id"], result["guardian_mission_id"]
    _start_and_mark(client, eid, clock, {"GRD-001": "present", "GRD-002": "present", "GRD-003": "absent"})
    clock.now = START + timedelta(minutes=15)
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert [(s.action_type, s.tool_name) for s in steps] == [
        ("tool", "get_exam_absentees"), ("tool", "request_exam_followup"), ("wait", None)]
    sid = small_class["students"]["GRD-003"]
    assert [f.student_id for f in exam_followups(session_factory, eid, "absence_followup")] == [sid]
    assert all(r["model"] == "gpt-oss:20b" for r in fake.requests)


def test_attendance_guardian_runs_on_the_local_brain(client, small_class, app, clock, session_factory, orgs,
                                                     no_network) -> None:
    fake = FakeOllama()
    guardians_on(app, fake)
    absent_class(client, clock, session_factory, orgs, small_class, third="absent")
    [case] = interventions(session_factory)
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, case.mission_id)
    assert [(s.action_type, s.tool_name) for s in steps] == [
        ("tool", "get_attendance_case"), ("tool", "request_attendance_followup"), ("wait", None)]
    assert mission.status == AgentMissionStatus.WAITING_EVENT and len(fake.requests) == 3


def test_a_supervised_guardian_cannot_be_completed_by_the_local_model(client, small_class, app, clock,
                                                                      session_factory) -> None:
    def claims_done(payload):
        return AgentDecision(kind=DecisionKind.COMPLETE, outcome="All done.")

    guardians_on(app, FakeOllama(claims_done))
    result = published(client, small_class)
    clock.advance(minutes=1)
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, result["guardian_mission_id"])
    assert mission.status == AgentMissionStatus.WAITING_EVENT  # the supervisor keeps monitoring
    assert (steps[0].status.value, steps[0].output_summary["error_code"]) == ("rejected", "COMPLETION_NOT_VERIFIED")


# --- 14-16: communication offline ----------------------------------------------------------------------------------


def voice_jobs(app, client, clock, small_class, session_factory, orgs, codes=("GRD-001",)):
    """Students consent to voice with a phone; the (local) Communication Agent chooses voice; jobs are READY."""
    exotel = FakeExotel()
    connectors = ConnectorRegistry([ExotelVoiceConnector(CONFIG, client=exotel, policy=VOICE_POLICY)])
    from tests.test_agentos_communication import use_runtime
    use_runtime(app, brain=local_router(app, FakeOllama(), net=Net(True))[0], connectors=connectors, policy=VOICE_POLICY)
    with tenant(session_factory, orgs["a"]) as s:
        for code in codes:
            student = small_class["students"][code]
            s.add(CommunicationPreference(student_id=student, allow_voice=True, preferred_channel="voice"))
            s.add(ContactPoint(student_id=student, kind=ContactKind.PHONE, address=PHONE, verified=True))
        s.commit()
    requested(client, small_class, session_factory, orgs, codes=codes)
    comm(app, clock)
    for job in jobs(session_factory):
        advance(app, orgs["a"], job.agent_mission_id)
    assert {(j.status, j.selected_channel) for j in jobs(session_factory)} == {(CommunicationJobStatus.READY, "voice")}
    return exotel


def connectivity_events(session_factory):
    return [e.event_type for e in rows(session_factory, DomainEvent, DomainEvent.subject_type == "connectivity")]


def test_cloud_communication_is_deferred_offline_and_not_failed(app, client, clock, small_class, session_factory,
                                                                orgs) -> None:
    exotel = voice_jobs(app, client, clock, small_class, session_factory, orgs)
    net = Net(online=False)
    report = comm(app, clock, connectivity=net)
    assert report.connectivity == "local_only" and report.connectivity_events == ["NETWORK_LOST"] * 2  # per org
    assert [(r.outcome, r.code) for r in report.runs] == [("waiting_connectivity", WAITING_CONNECTIVITY)]
    (job,) = jobs(session_factory)
    assert (job.status, job.last_error_code, job.next_attempt_at, job.attempt_count) == (
        CommunicationJobStatus.DEFERRED, WAITING_CONNECTIVITY, None, 0)
    assert exotel.calls == []  # never attempted, never "delivered"
    guardian = rows(session_factory, AgentMission, AgentMission.agent_key == "assignment_guardian")[0]
    agent = rows(session_factory, AgentMission, AgentMission.id == job.agent_mission_id)[0]
    assert guardian.status != AgentMissionStatus.FAILED and agent.status != AgentMissionStatus.FAILED


def test_no_retry_storm_while_offline(app, client, clock, small_class, session_factory, orgs) -> None:
    voice_jobs(app, client, clock, small_class, session_factory, orgs)
    net = Net(online=False)
    comm(app, clock, connectivity=net)
    for _ in range(4):
        clock.advance(minutes=30)
        assert comm(app, clock, connectivity=net).runs == []  # the waiting job is not due
    (job,) = jobs(session_factory)
    assert job.attempt_count == 0 and connectivity_events(session_factory) == ["NETWORK_LOST"] * 2  # no spam
    audits = rows(session_factory, OperationAuditEvent, OperationAuditEvent.event_type == "COMMUNICATION_WAITING_CONNECTIVITY")
    assert len(audits) == 1


def test_network_restored_makes_deferred_jobs_eligible_in_staggered_batches(app, client, clock, small_class,
                                                                             session_factory, orgs) -> None:
    exotel = voice_jobs(app, client, clock, small_class, session_factory, orgs, codes=("GRD-001", "GRD-002", "GRD-003"))
    net = Net(online=False)
    comm(app, clock, connectivity=net)
    assert {j.last_error_code for j in jobs(session_factory)} == {WAITING_CONNECTIVITY}
    clock.advance(minutes=10)
    net.online = True
    report = process_communication_jobs(app.state.session_factory, app.state.agent_runtime, now=clock.now, limit=1,
                                        connectivity=net)
    assert report.connectivity_events == ["NETWORK_RESTORED"] * 2 and report.released == 3
    assert [(r.outcome) for r in report.runs] == ["in_progress"] and len(exotel.calls) == 1  # one, not three
    waits = sorted(j.next_attempt_at for j in jobs(session_factory) if j.status == CommunicationJobStatus.DEFERRED)
    assert waits == [clock.now + RELEASE_SPACING, clock.now + 2 * RELEASE_SPACING]
    assert all(e.consumed_at is not None for e in rows(session_factory, DomainEvent,
                                                       DomainEvent.event_type == "NETWORK_RESTORED"))
    assert comm(app, clock, connectivity=net).connectivity_events == []  # still online: nothing new published
    clock.advance(minutes=2)
    process_communication_jobs(app.state.session_factory, app.state.agent_runtime, now=clock.now, limit=5,
                               connectivity=net)
    assert len(exotel.calls) == 3


def test_in_app_follow_up_is_delivered_offline(app, client, clock, small_class, session_factory, orgs) -> None:
    from tests.test_agentos_communication import use_runtime
    use_runtime(app, brain=local_router(app, FakeOllama())[0])
    job = ready_job(app, client, clock, small_class, session_factory, orgs)
    report = comm(app, clock, connectivity=Net(online=False))
    assert [(r.job_id, r.outcome) for r in report.runs] == [(job.id, "delivered")]
    assert len(notifications(session_factory, job.recipient_student_id)) == 1


def test_connectivity_transitions_are_published_only_on_change(session_factory, orgs, clock) -> None:
    with tenant(session_factory, orgs["a"]) as s:
        assert publish_transition(s, ConnectivityState.CLOUD_REACHABLE, clock.now) is None  # online is the default
        assert publish_transition(s, ConnectivityState.UNKNOWN, clock.now) is None
        assert publish_transition(s, ConnectivityState.LOCAL_ONLY, clock.now).value == "NETWORK_LOST"
        assert publish_transition(s, ConnectivityState.LOCAL_ONLY, clock.now) is None
        assert publish_transition(s, ConnectivityState.UNKNOWN, clock.now) is None
        assert publish_transition(s, ConnectivityState.CLOUD_REACHABLE, clock.now).value == "NETWORK_RESTORED"
        assert publish_transition(s, ConnectivityState.CLOUD_REACHABLE, clock.now) is None
        s.commit()
    with tenant(session_factory, orgs["b"]) as s:  # per organization: B has seen nothing
        assert publish_transition(s, ConnectivityState.CLOUD_REACHABLE, clock.now) is None


def test_connectivity_service_is_cached_bounded_and_never_stacks_probes() -> None:
    import threading
    import time as _time

    now = [0.0]
    calls = []
    service = ConnectivityService(lambda: calls.append(1) or True, cache_seconds=30, timeout=0.2, clock=lambda: now[0])
    assert service.state() == ConnectivityState.CLOUD_REACHABLE
    for _ in range(50):
        service.state()
    assert service.probes == 1
    now[0] = 31.0
    service.state()
    assert service.probes == 2
    service.invalidate()
    service.state()
    assert service.probes == 3

    release = threading.Event()
    slow = ConnectivityService(lambda: release.wait(5) or True, timeout=0.2)
    started = _time.perf_counter()
    assert slow.state() == ConnectivityState.UNKNOWN  # did not answer in time
    assert _time.perf_counter() - started < 2
    slow.invalidate()
    assert slow.state() == ConnectivityState.UNKNOWN and slow.probes == 1  # the hung probe is not stacked
    release.set()
    assert ConnectivityService(None).state() == ConnectivityState.UNKNOWN  # not configured: never probes


# --- 17-19: local speech subprocesses -------------------------------------------------------------------------------


class Runner:
    def __init__(self, stdout: bytes = b"", returncode: int = 0, error=None) -> None:
        self.stdout, self.returncode, self.error, self.calls = stdout, returncode, error, []

    def __call__(self, argv, **kwargs):
        wav_path = argv[argv.index("-f") + 1] if "-f" in argv else None
        self.calls.append({"argv": argv, "kwargs": kwargs, "file_existed": wav_path and os.path.exists(wav_path)})
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, b"stderr may echo input")


@pytest.fixture()
def binaries(tmp_path):
    files = {}
    for name in ("whisper-cli.exe", "ggml-base.en.bin", "piper.exe", "voice.onnx"):
        path = tmp_path / name
        path.write_bytes(b"x")
        files[name] = path
    (tmp_path / "voice.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 22050}}))
    return files


def test_whisper_subprocess_has_fixed_safe_args_and_deletes_its_temp_file(binaries) -> None:
    runner = Runner(b"  who am i [BLANK_AUDIO] \n")
    stt = LocalWhisperSTTProvider(WhisperConfig(binaries["whisper-cli.exe"], binaries["ggml-base.en.bin"], "en", 9.0),
                                  runner=runner)
    transcript = stt.transcribe(b"\x01\x00" * 16000)
    assert (transcript.text, transcript.usable) == ("who am i", True) and "who am i" not in repr(transcript)
    (call,) = runner.calls
    wav_path = call["argv"][4]
    assert call["argv"] == whisper_argv(binaries["whisper-cli.exe"], binaries["ggml-base.en.bin"], wav_path, "en")
    assert call["argv"][1:] == ["-m", str(binaries["ggml-base.en.bin"]), "-f", wav_path, "-l", "en", "-nt", "-np"]
    assert call["kwargs"]["shell"] is False and call["kwargs"]["timeout"] == 9.0 and call["file_existed"]
    assert not os.path.exists(wav_path)  # deleted right after the run
    assert set(call["kwargs"]["env"]) <= {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}


@pytest.mark.parametrize("runner, code", [
    (Runner(returncode=1), "LOCAL_SPEECH_FAILED"),
    (Runner(error=subprocess.TimeoutExpired("whisper", 9)), "LOCAL_SPEECH_TIMEOUT"),
    (Runner(error=FileNotFoundError()), "LOCAL_SPEECH_UNAVAILABLE"),
    (Runner(stdout=b"x" * 70000), "LOCAL_SPEECH_OUTPUT_TOO_LARGE"),
])
def test_whisper_failures_are_codes_and_the_temp_file_is_still_deleted(binaries, runner, code) -> None:
    stt = LocalWhisperSTTProvider(WhisperConfig(binaries["whisper-cli.exe"], binaries["ggml-base.en.bin"]), runner=runner)
    with pytest.raises(LocalToolError) as exc:
        stt.transcribe(b"\x01\x00" * 1600)
    assert exc.value.code == code and "stderr" not in str(exc.value)
    assert not os.path.exists(runner.calls[0]["argv"][4])


def test_whisper_bounds_and_configuration(binaries, monkeypatch) -> None:
    stt = LocalWhisperSTTProvider(WhisperConfig(binaries["whisper-cli.exe"], binaries["ggml-base.en.bin"]), runner=Runner())
    with pytest.raises(LocalToolError) as exc:
        stt.transcribe(b"\x00\x00" * SAMPLE_RATE * 31)
    assert exc.value.code == "STT_AUDIO_TOO_LONG"
    monkeypatch.setenv("CAMPUSNEXUS_WHISPER_CPP_PATH", "whisper-cli")  # relative: never resolved through PATH
    monkeypatch.setenv("CAMPUSNEXUS_WHISPER_MODEL_PATH", str(binaries["ggml-base.en.bin"]))
    assert WhisperConfig.from_env().unavailable_reason() == "WHISPER_CPP_NOT_CONFIGURED"
    assert trusted_file("CAMPUSNEXUS_WHISPER_MODEL_PATH") == binaries["ggml-base.en.bin"]
    monkeypatch.setenv("CAMPUSNEXUS_STT_LANGUAGE", "en -m /tmp/evil")
    with pytest.raises(ValueError):
        WhisperConfig.from_env()
    assert not LocalWhisperSTTProvider(WhisperConfig(None, None)).available()


def test_piper_subprocess_has_fixed_safe_args_and_text_only_on_stdin(binaries) -> None:
    raw = (b"\x10\x00\x20\x00" * 11025)  # 1 s at 22050 Hz
    runner = Runner(raw)
    tts = PiperTTSProvider(PiperConfig(binaries["piper.exe"], binaries["voice.onnx"], 7.0), runner=runner)
    hostile = "Hello --output_file C:/evil.wav; rm -rf / && calc.exe\x00\x07"
    pcm = tts.synthesize(hostile)
    (call,) = runner.calls
    assert call["argv"] == piper_argv(binaries["piper.exe"], binaries["voice.onnx"])
    assert call["argv"][1:] == ["--model", str(binaries["voice.onnx"]), "--output_raw"]
    assert call["kwargs"]["shell"] is False and call["kwargs"]["timeout"] == 7.0
    assert call["kwargs"]["input"] == b"Hello --output_file C:/evil.wav; rm -rf / && calc.exe\n"  # stdin only
    assert abs(len(pcm) - 2 * SAMPLE_RATE) <= 4  # normalized to 16 kHz linear16
    with pytest.raises(LocalToolError) as exc:
        tts.synthesize("x" * 401)
    assert exc.value.code == "TTS_INPUT_TOO_LONG"
    assert PiperTTSProvider(PiperConfig(None, None)).available() is False


def test_resampling_keeps_linear16_format() -> None:
    assert resample_linear16(b"\x00\x00" * 22050, 22050) == b"\x00\x00" * 16000
    assert resample_linear16(b"\x01\x00" * 100, 16000) == b"\x01\x00" * 100


# --- 20-22: the Nexus voice endpoint --------------------------------------------------------------------------------


class FakeLocalSTT:
    provider_name, model_name = "whisper_cpp", "whisper.cpp"

    def __init__(self, text: str = SPOKEN, usable: bool = True) -> None:
        self.text, self.usable, self.calls = text, usable, 0

    def available(self) -> bool:
        return True

    def transcribe(self, pcm: bytes) -> Transcript:
        self.calls += 1
        return Transcript(self.text, self.usable)


class FakeLocalTTS:
    provider_name, model_name = "piper", "piper"

    def __init__(self, error: str | None = None) -> None:
        self.error, self.texts = error, []

    def available(self) -> bool:
        return True

    def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        if self.error:
            raise SpeechProviderError(self.error)
        return b"\x05\x00" * 1600


def wav(seconds: float = 1.0, rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(rate * seconds) * channels)
    return buffer.getvalue()


def voice_app(app, *, stt=None, tts=None, mode="local", cloud_stt=None):
    fake = FakeOllama(nexus_policy)
    router, net = local_router(app, fake)
    use_brain(app, router)
    stt, tts = stt or FakeLocalSTT(), tts or FakeLocalTTS()
    app.state.speech_router = SpeechRouter(
        stt_mode=mode, tts_mode=mode, connectivity=net, cloud_stt=cloud_stt,
        local_stt=MeteredSTT(stt, "whisper_cpp", "whisper.cpp"), local_tts=MeteredTTS(tts, "piper", "piper"))
    return fake, stt, tts


def speak(client, who, body=None, content_type="audio/wav", **params):
    headers = {"Content-Type": content_type}
    if who:
        headers.update(bearer(login(client, who)["access_token"]))
    return client.post("/agentos/assistant/voice", content=body if body is not None else wav(), headers=headers,
                       params=params)


def test_voice_endpoint_runs_nexus_for_the_authenticated_user_only(app, client, session_factory, no_network) -> None:
    fake, stt, tts = voice_app(app)
    response = speak(client, A_STUDENT)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed" and body["speech"] == {"stt_provider": "whisper_cpp", "tts_provider": "piper"}
    assert body["brain"]["provider"] == "ollama" and "Pending assignments" in body["assistant_message"]
    pcm = wav_to_pcm(base64.b64decode(body["audio_wav_base64"]), max_bytes=10 ** 6)
    assert pcm == b"\x05\x00" * 1600 and tts.texts == [body["assistant_message"]]
    assert not ({"transcript", "reasoning", "thinking", "goal"} & set(body))
    mission, steps, audits = mission_rows(session_factory, body["mission_id"])
    assert mission.owner_account_id == account_id(session_factory, A_STUDENT) and mission.goal == UNSTORED_GOAL
    assert [s.tool_name for s in steps][:2] == ["get_my_identity_context", "get_my_assignments"]
    assert SPOKEN in fake.requests[0]["messages"][1]["content"]  # the brain heard the request (from memory only)
    assert [a.event_metadata["input"] for a in audits if a.event_type == "NEXUS_REQUEST_RECEIVED"] == ["voice"]
    other = client.get(f"/agentos/missions/{body['mission_id']}", headers=bearer(login(client, B_STUDENT)["access_token"]))
    assert other.status_code == 404
    assert speak(client, None).status_code == 401
    smuggled = speak(client, A_STUDENT, student_id="STU-999", organization_id=999)
    assert rows(session_factory, AgentMission, AgentMission.id == smuggled.json()["mission_id"])[0].owner_account_id == \
        account_id(session_factory, A_STUDENT)


def test_transcript_and_audio_are_never_persisted(app, client, session_factory) -> None:
    voice_app(app)
    body = speak(client, A_STUDENT).json()
    stored = db_text(session_factory)
    assert SPOKEN not in stored and SPOKEN.lower() not in stored.lower()
    assert body["audio_wav_base64"] not in stored and THINKING not in stored


@pytest.mark.parametrize("payload, content_type, status, code", [
    (None, "application/json", 415, "AUDIO_UNSUPPORTED_TYPE"),
    ("long", "audio/wav", 413, "AUDIO_TOO_LARGE"),
    ("rate", "audio/wav", 400, "AUDIO_WRONG_SAMPLE_RATE"),
    ("stereo", "audio/wav", 400, "AUDIO_NOT_MONO"),
    ("junk", "audio/wav", 400, "AUDIO_NOT_WAV"),
])
def test_voice_upload_is_bounded_and_strict(app, client, payload, content_type, status, code) -> None:
    _, stt, _ = voice_app(app)
    body = {None: wav(), "long": wav(31), "rate": wav(rate=8000), "stereo": wav(channels=2), "junk": b"RIFF" + b"x" * 64}
    response = speak(client, A_STUDENT, body[payload], content_type)
    assert response.status_code == status and response.json()["detail"]["code"] == code
    assert stt.calls == 0  # nothing reached a speech provider


def test_unusable_speech_or_stt_failure_creates_no_mission(app, client, session_factory) -> None:
    voice_app(app, stt=FakeLocalSTT("", usable=False))
    response = speak(client, A_STUDENT)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "SPEECH_NOT_UNDERSTOOD"
    assert rows(session_factory, AgentMission) == []
    app.state.speech_router = SpeechRouter(stt_mode="local", tts_mode="local", connectivity=Net(False),
                                           local_stt=MeteredSTT(LocalWhisperSTTProvider(WhisperConfig(None, None)),
                                                                "whisper_cpp", "whisper.cpp"))
    response = speak(client, A_STUDENT)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "LOCAL_STT_UNAVAILABLE"


def test_tts_failure_still_returns_the_text_reply(app, client) -> None:
    voice_app(app, tts=FakeLocalTTS(error="LOCAL_SPEECH_TIMEOUT"))
    body = speak(client, A_STUDENT).json()
    assert body["assistant_message"] and body["audio_wav_base64"] is None
    assert body["audio_error_code"] == "LOCAL_SPEECH_TIMEOUT"


def test_speech_auto_mode_uses_local_offline_and_never_fakes(app) -> None:
    cloud = MeteredSTT(FakeLocalSTT("cloud words"), "groq", "whisper-large-v3-turbo")
    local = MeteredSTT(FakeLocalSTT("local words"), "whisper_cpp", "whisper.cpp")
    net = Net(online=False)
    router = SpeechRouter(stt_mode="auto", tts_mode="auto", connectivity=net, cloud_stt=cloud, local_stt=local)
    assert router.transcribe(b"\x00\x00")[1] == "whisper_cpp"
    net.online = True
    assert router.transcribe(b"\x00\x00")[1] == "groq"
    with pytest.raises(SpeechProviderError) as exc:
        router.synthesize("hi")  # no TTS configured at all: unavailable, never a fake voice
    assert exc.value.code == "LOCAL_TTS_UNAVAILABLE"
    cloud_only = SpeechRouter(stt_mode="cloud", tts_mode="cloud", connectivity=Net(False), local_stt=local)
    with pytest.raises(SpeechProviderError) as exc:
        cloud_only.transcribe(b"\x00\x00")
    assert exc.value.code == "CLOUD_STT_NOT_CONFIGURED"


def test_local_and_voice_telemetry_hold_no_content(app, client, session_factory) -> None:
    voice_app(app)
    body = speak(client, A_STUDENT).json()
    usage = rows(session_factory, AIUsageEvent)
    by_op = {}
    for u in usage:
        by_op.setdefault(u.operation, []).append(u)
    assert {u.provider for u in by_op["voice_stt"]} == {"whisper_cpp"} and by_op["voice_stt"][0].audio_ms == 1000
    assert {u.provider for u in by_op["voice_tts"]} == {"piper"} and by_op["voice_tts"][0].audio_ms == 100
    assert {u.provider for u in by_op["agent_brain_decide"]} == {"ollama"}
    assert all(u.estimated_cost_usd is None and u.success for u in usage)
    text = " ".join(repr((u.operation, u.model, u.provider, u.mission_id, u.agent_key, u.run_id, u.error_kind))
                    for u in usage)
    assert SPOKEN not in text and body["assistant_message"] not in text and THINKING not in text


def test_health_reports_edge_status_without_urls_paths_or_keys(app, client, monkeypatch, binaries) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret_health_key")
    voice_app(app)
    status = client.get("/health").json()
    assert status["intelligence"] == {"mode": "local", "cloud_available": False, "local_available": True,
                                      "active_provider": "ollama"}
    assert status["speech"] == {"stt_provider": "whisper_cpp", "tts_provider": "piper", "local_ready": True}
    assert status["connectivity"] in ("online", "offline", "unknown") and status["database"]["type"] == "sqlite"
    text = json.dumps(status)
    for forbidden in ("http://", "https://", "127.0.0.1", "11434", "gsk_secret", str(binaries["piper.exe"].parent)):
        assert forbidden not in text
