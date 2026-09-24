"""Offline tests for AnthropicLLMProvider (Phase 9 live-mode hardening).

No network and no live model: the provider's ``client=`` seam takes a fake
exposing ``messages.create``. One test additionally drives the *real*
``anthropic`` SDK against a local stub HTTP server (skipped when the SDK
isn't installed) to prove request serialization and response parsing work
through the genuine client, not just the fake.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest

from app.graph.registry import AgentRegistry
from app.graph.validator import validate_plan
from app.llm.base import LLMProviderError
from app.llm.factory import get_llm_provider
from app.llm.providers.anthropic_provider import AnthropicLLMProvider
from app.schemas.academic import AcademicIntent
from app.schemas.enums import AgentName
from app.schemas.events import EventsIntent

ALL_AGENTS = [
    AgentName.ACADEMIC_AGENT,
    AgentName.CAREER_AGENT,
    AgentName.EVENTS_OPPORTUNITY_AGENT,
    AgentName.CAMPUS_SERVICES_AGENT,
    AgentName.ACTION_AGENT,
]


class FakeMessages:
    def __init__(self, respond: Callable[[Dict[str, Any]], Any]) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._respond = respond

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self._respond(kwargs)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, respond: Callable[[Dict[str, Any]], Any]) -> None:
        self.messages = FakeMessages(respond)


def tool_response(name: str, payload: Dict[str, Any], stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="tool_use", name=name, input=payload)])


def text_response(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def provider_returning(response: Any) -> AnthropicLLMProvider:
    return AnthropicLLMProvider(model="claude-sonnet-5", client=FakeClient(lambda _kwargs: response))


def _registry() -> AgentRegistry:
    registry = AgentRegistry()
    for agent in ALL_AGENTS:
        registry.register(agent, lambda s: None)  # validate_plan only asks is_supported()
    return registry


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_every_request_disables_thinking_and_forces_the_structured_tool() -> None:
    provider = provider_returning(tool_response("classify_academic_intent", {"intent": "attendance_status"}))
    result = provider.classify_academic_intent("What's my OS attendance?", [])
    assert result.intent == AcademicIntent.ATTENDANCE_STATUS

    call = provider._client.messages.calls[0]
    # Sonnet 5 runs adaptive thinking when `thinking` is omitted, and
    # max_tokens caps thinking + output -- so it must be explicit.
    assert call["thinking"] == {"type": "disabled"}
    assert call["max_tokens"] >= 1024
    assert call["tool_choice"] == {"type": "tool", "name": "classify_academic_intent"}
    assert call["model"] == "claude-sonnet-5"


def test_response_generation_also_disables_thinking() -> None:
    provider = provider_returning(text_response("Here are your events."))
    text = provider._text(system="s", context=SimpleNamespace(model_dump=lambda mode: {"x": 1}))  # type: ignore[arg-type]
    assert text == "Here are your events."
    call = provider._client.messages.calls[0]
    assert call["thinking"] == {"type": "disabled"}
    assert "tools" not in call


def test_planner_prompt_describes_only_the_supported_agents() -> None:
    provider = provider_returning(tool_response("produce_mission_plan", {"tasks": []}))
    provider.plan_mission("m-1", "anything", supported_agents=[AgentName.ACADEMIC_AGENT, AgentName.CAREER_AGENT])
    content = provider._client.messages.calls[0]["messages"][0]["content"]
    assert "- academic_agent:" in content and "- career_agent:" in content
    assert "action_agent" not in content and "events_opportunity_agent" not in content


# ---------------------------------------------------------------------------
# Plan conversion: typed action constraints, unsupported requests, no silent repair
# ---------------------------------------------------------------------------


def test_plan_proposal_becomes_a_valid_dag_with_action_constraints() -> None:
    proposal = {
        "tasks": [
            {"index": 1, "objective": "What is my timetable?", "agent": "academic_agent", "requires_evidence": False},
            {"index": 2, "objective": "When are my exams?", "agent": "academic_agent", "requires_evidence": False},
            {
                "index": 3,
                "objective": "Find the 'Competitive Coding Contest' and check clashes",
                "agent": "events_opportunity_agent",
                "depends_on_indices": [1, 2],
                # constraints on a non-action task are never forwarded
                "action": {"tool_name": "register_event", "event_title": "ignored"},
            },
            {
                "index": 4,
                "objective": "Register for 'Competitive Coding Contest'",
                "agent": "action_agent",
                "depends_on_indices": [3],
                "action": {"tool_name": "register_event", "event_title": "Competitive Coding Contest"},
            },
        ],
        "unsupported_requests": ["Booking travel is not supported."],
    }
    plan = provider_returning(tool_response("produce_mission_plan", proposal)).plan_mission(
        "m-2", "goal", supported_agents=ALL_AGENTS
    )

    assert [t.task_id for t in plan.tasks] == ["m-2-task-1", "m-2-task-2", "m-2-task-3", "m-2-task-4"]
    assert plan.tasks[2].dependencies == ["m-2-task-1", "m-2-task-2"]
    assert plan.tasks[2].constraints == {}
    assert plan.tasks[3].constraints == {"tool_name": "register_event", "event_title": "Competitive Coding Contest"}
    assert plan.unsupported_requests == ["Booking travel is not supported."]
    assert validate_plan(plan, _registry(), expected_mission_id="m-2").is_valid


def test_dangling_dependency_index_is_rejected_by_the_validator_not_silently_dropped() -> None:
    proposal = {"tasks": [{"index": 1, "objective": "When are my exams?", "agent": "academic_agent", "depends_on_indices": [7]}]}
    plan = provider_returning(tool_response("produce_mission_plan", proposal)).plan_mission(
        "m-3", "goal", supported_agents=ALL_AGENTS
    )
    assert plan.tasks[0].dependencies == ["m-3-task-7"]
    result = validate_plan(plan, _registry(), expected_mission_id="m-3")
    assert not result.is_valid
    assert any("unknown task id" in error for error in result.errors)


def test_cyclic_proposal_is_rejected_by_the_validator() -> None:
    proposal = {
        "tasks": [
            {"index": 1, "objective": "When are my exams?", "agent": "academic_agent", "depends_on_indices": [2]},
            {"index": 2, "objective": "What is my timetable?", "agent": "academic_agent", "depends_on_indices": [1]},
        ]
    }
    plan = provider_returning(tool_response("produce_mission_plan", proposal)).plan_mission(
        "m-4", "goal", supported_agents=ALL_AGENTS
    )
    assert not validate_plan(plan, _registry(), expected_mission_id="m-4").is_valid


def test_unregistered_agent_in_proposal_is_rejected_by_the_validator() -> None:
    proposal = {"tasks": [{"index": 1, "objective": "Look it up", "agent": "knowledge_rag_agent"}]}
    plan = provider_returning(tool_response("produce_mission_plan", proposal)).plan_mission(
        "m-5", "goal", supported_agents=ALL_AGENTS
    )
    result = validate_plan(plan, _registry(), expected_mission_id="m-5")
    assert not result.is_valid
    assert any("unsupported agent" in error for error in result.errors)


def test_self_dependency_raises_a_provider_error() -> None:
    proposal = {"tasks": [{"index": 1, "objective": "When are my exams?", "agent": "academic_agent", "depends_on_indices": [1]}]}
    with pytest.raises(LLMProviderError, match="structurally invalid"):
        provider_returning(tool_response("produce_mission_plan", proposal)).plan_mission("m-6", "g", supported_agents=ALL_AGENTS)


def test_invented_agent_name_fails_schema_validation() -> None:
    proposal = {"tasks": [{"index": 1, "objective": "Book a flight", "agent": "travel_agent"}]}
    with pytest.raises(LLMProviderError, match="schema validation"):
        provider_returning(tool_response("produce_mission_plan", proposal)).plan_mission("m-7", "g", supported_agents=ALL_AGENTS)


def test_invented_action_tool_fails_schema_validation() -> None:
    proposal = {"tasks": [{"index": 1, "objective": "Pay my fees", "agent": "action_agent", "action": {"tool_name": "pay_fees"}}]}
    with pytest.raises(LLMProviderError, match="schema validation"):
        provider_returning(tool_response("produce_mission_plan", proposal)).plan_mission("m-8", "g", supported_agents=ALL_AGENTS)


# ---------------------------------------------------------------------------
# Failures always raise LLMProviderError -- never a mock/default answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_truncated_or_refused_responses_raise(stop_reason: str) -> None:
    provider = provider_returning(tool_response("classify_events_intent", {"intent": "event_discovery"}, stop_reason=stop_reason))
    with pytest.raises(LLMProviderError):
        provider.classify_events_intent("Find workshops")


def test_missing_tool_call_raises() -> None:
    with pytest.raises(LLMProviderError, match="did not include"):
        provider_returning(text_response("I think it's event discovery")).classify_events_intent("Find workshops")


def test_transport_error_is_wrapped_and_chained() -> None:
    boom = ConnectionError("network unreachable")
    provider = AnthropicLLMProvider(model="claude-sonnet-5", client=FakeClient(lambda _kwargs: boom))
    with pytest.raises(LLMProviderError, match="network unreachable") as info:
        provider.classify_career_intent("Find internships")
    assert info.value.__cause__ is boom


def test_empty_text_response_raises() -> None:
    provider = provider_returning(text_response("   "))
    with pytest.raises(LLMProviderError, match="empty"):
        provider._text(system="s", context=SimpleNamespace(model_dump=lambda mode: {}))  # type: ignore[arg-type]


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-opus-5-5", "claude-mythos-5-1"])
def test_incompatible_models_are_rejected_at_construction(model: str) -> None:
    with pytest.raises(LLMProviderError, match="not supported"):
        AnthropicLLMProvider(model=model, client=FakeClient(lambda _kwargs: None))


def test_missing_api_key_fails_clearly_and_factory_never_falls_back_to_mock(monkeypatch) -> None:
    pytest.importorskip("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMProviderError, match="ANTHROPIC_API_KEY"):
        get_llm_provider("anthropic")


def test_missing_sdk_fails_with_install_hint(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` raise ImportError
    with pytest.raises(LLMProviderError, match=r'pip install -e "\.\[dev,llm\]"'):
        AnthropicLLMProvider(model="claude-sonnet-5", api_key="unused")


def test_provider_reports_itself_as_live() -> None:
    provider = provider_returning(None)
    assert provider.is_live is True
    assert provider.model_name == "claude-sonnet-5"
    assert get_llm_provider("mock").is_live is False


# ---------------------------------------------------------------------------
# Real SDK against a local stub server (no network, no key)
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    requests: List[Dict[str, Any]] = []
    status = 200

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        if type(self).status != 200:
            payload = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        else:
            payload = {
                "id": "msg_stub", "type": "message", "role": "assistant", "model": body["model"],
                "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
                "content": [{"type": "tool_use", "id": "toolu_stub", "name": body["tool_choice"]["name"],
                             "input": {"intent": "event_discovery"}}],
            }
        data = json.dumps(payload).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def stub_server():
    _StubHandler.requests = []
    _StubHandler.status = 200
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_real_sdk_round_trip_against_stub_server(stub_server) -> None:
    anthropic = pytest.importorskip("anthropic")
    client = anthropic.Anthropic(api_key="test-key", base_url=stub_server, max_retries=0)
    provider = AnthropicLLMProvider(model="claude-sonnet-5", client=client)

    result = provider.classify_events_intent("Find workshops about machine learning")

    assert result.intent == EventsIntent.EVENT_DISCOVERY
    sent = _StubHandler.requests[0]
    assert sent["thinking"] == {"type": "disabled"}
    assert sent["tools"][0]["name"] == "classify_events_intent"
    assert sent["tools"][0]["input_schema"]["type"] == "object"


def test_real_sdk_auth_error_becomes_provider_error(stub_server) -> None:
    anthropic = pytest.importorskip("anthropic")
    _StubHandler.status = 401
    client = anthropic.Anthropic(api_key="bad-key", base_url=stub_server, max_retries=0)
    provider = AnthropicLLMProvider(model="claude-sonnet-5", client=client)
    with pytest.raises(LLMProviderError, match="AuthenticationError"):
        provider.classify_events_intent("Find workshops")


def test_real_sdk_connection_failure_becomes_provider_error() -> None:
    anthropic = pytest.importorskip("anthropic")
    client = anthropic.Anthropic(api_key="test-key", base_url="http://127.0.0.1:9", max_retries=0, timeout=2)
    provider = AnthropicLLMProvider(model="claude-sonnet-5", client=client)
    with pytest.raises(LLMProviderError) as info:
        provider.classify_events_intent("Find workshops")
    # Refused (APIConnectionError) or hung until timeout (APITimeoutError,
    # its subclass) depending on the OS -- either way, a connection failure.
    assert isinstance(info.value.__cause__, anthropic.APIConnectionError)
