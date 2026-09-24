"""Offline tests for GroqLLMProvider and check_live_llm.py --smoke (Phase 12).

No network and no real key: every request goes through a genuine
``httpx.Client`` whose transport is an ``httpx.MockTransport`` returning
scripted OpenAI-compatible Chat Completions responses, so request
serialization, status handling and response parsing are all exercised.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import pytest
from pydantic import BaseModel

httpx = pytest.importorskip("httpx")

from app.graph.registry import AgentRegistry
from app.graph.validator import validate_plan
from app.llm.base import LLMProviderError
from app.llm.factory import get_llm_provider
from app.llm.providers.groq import DEFAULT_MODEL, GroqLLMProvider, _inline_refs
from app.llm.providers.mock import MockLLMProvider
from app.schemas.academic import AcademicIntent, AcademicIntentResult
from app.schemas.career import CareerIntent
from app.schemas.enums import AgentName
from app.schemas.events import EventsIntent
from app.schemas.services import ServicesIntent
from scripts import check_live_llm

ALL_AGENTS = check_live_llm.ALL_AGENTS
SECRET = "gsk_test_secret_key_value"


class _Context(BaseModel):
    x: int = 1


# ---------------------------------------------------------------------------
# Scripted Groq server
# ---------------------------------------------------------------------------


def tool_call(name: str, arguments: Any, finish_reason: str = "tool_calls") -> Dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    message = {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": raw}}]}
    return {"choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]}


def text_reply(text: Optional[str], finish_reason: str = "stop") -> Dict[str, Any]:
    return {"choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": text}}]}


def error_body(status: int, message: str, code: str = "", headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message, "type": "invalid_request_error", "code": code}}, headers=headers)


class ScriptedGroq:
    """Records every request and answers from ``respond(body)`` -- a dict
    (200 JSON), an ``httpx.Response``, or an exception to raise."""

    def __init__(self, respond: Callable[[Dict[str, Any]], Any]) -> None:
        self.respond = respond
        self.requests: List[httpx.Request] = []
        self.bodies: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(request)
        self.bodies.append(body)
        result = self.respond(body)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)


def provider_for(respond: Callable[[Dict[str, Any]], Any], **kwargs: Any) -> tuple[GroqLLMProvider, ScriptedGroq, List[float]]:
    server = ScriptedGroq(respond)
    client = httpx.Client(
        base_url="https://groq.test/openai/v1",
        headers={"Authorization": f"Bearer {SECRET}"},
        transport=httpx.MockTransport(server),
    )
    sleeps: List[float] = []
    provider = GroqLLMProvider(
        model=kwargs.pop("model", DEFAULT_MODEL), api_key=SECRET, client=client, sleep=sleeps.append, **kwargs
    )
    return provider, server, sleeps


def _registry() -> AgentRegistry:
    registry = AgentRegistry()
    for agent in ALL_AGENTS:
        registry.register(agent, lambda s: None)
    return registry


ACTION_PROPOSAL = {
    "tasks": [
        {"index": 1, "objective": "Find the Competitive Coding Contest.", "agent": "events_opportunity_agent", "depends_on_indices": [], "requires_evidence": False},
        {"index": 2, "objective": "What is my timetable?", "agent": "academic_agent", "depends_on_indices": [], "requires_evidence": False},
        {"index": 3, "objective": "When are my exams?", "agent": "academic_agent", "depends_on_indices": [], "requires_evidence": False},
        {
            "index": 4, "objective": "Register for the Competitive Coding Contest.", "agent": "action_agent",
            "depends_on_indices": [1, 2, 3], "requires_evidence": False,
            "action": {"tool_name": "register_event", "event_title": "Competitive Coding Contest", "category": None},
        },
    ],
    "unsupported_requests": [],
}
MULTI_AGENT_PROPOSAL = {
    "tasks": [
        {"index": 1, "objective": "Find AI internships and my skill gaps.", "agent": "career_agent", "depends_on_indices": [], "requires_evidence": False},
        {"index": 2, "objective": "What is my timetable?", "agent": "academic_agent", "depends_on_indices": [], "requires_evidence": False},
    ],
    "unsupported_requests": [],
}
UNSUPPORTED_PROPOSAL = {"tasks": [], "unsupported_requests": ["Flight booking is not supported."]}


def smoke_server(body: Dict[str, Any]) -> Dict[str, Any]:
    """A well-behaved Groq that answers every --smoke check correctly."""
    tool = body["tool_choice"]["function"]["name"]
    goal = body["messages"][1]["content"]
    if tool == "classify_academic_intent":
        return tool_call(tool, {"intent": "exam_eligibility", "raw_course_reference": "OS"})
    if tool == "classify_career_intent":
        return tool_call(tool, {"intent": "opportunity_discovery"})
    if "Competitive Coding Contest" in goal:
        return tool_call(tool, ACTION_PROPOSAL)
    if "flight" in goal:
        return tool_call(tool, UNSUPPORTED_PROPOSAL)
    return tool_call(tool, MULTI_AGENT_PROPOSAL)


# ---------------------------------------------------------------------------
# Factory / configuration
# ---------------------------------------------------------------------------


def test_factory_builds_groq_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    monkeypatch.delenv("CAMPUSNEXUS_LLM_MODEL", raising=False)
    provider = get_llm_provider()
    assert isinstance(provider, GroqLLMProvider)
    assert provider.is_live and provider.name == "groq"
    assert provider.model_name == DEFAULT_MODEL


def test_model_is_configurable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    monkeypatch.setenv("CAMPUSNEXUS_LLM_MODEL", "llama-3.3-70b-versatile")
    assert get_llm_provider("groq").model_name == "llama-3.3-70b-versatile"


def test_missing_key_raises_and_never_falls_back_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMProviderError, match="GROQ_API_KEY"):
        get_llm_provider("groq")


def test_leftover_claude_model_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    monkeypatch.setenv("CAMPUSNEXUS_LLM_MODEL", "claude-sonnet-5")
    with pytest.raises(LLMProviderError, match="Anthropic model id"):
        get_llm_provider("groq")


def test_real_client_sends_bearer_key_to_groq_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    monkeypatch.delenv("CAMPUSNEXUS_LLM_MODEL", raising=False)
    provider = get_llm_provider("groq")
    assert str(provider._client.base_url).rstrip("/") == "https://api.groq.com/openai/v1"
    assert provider._client.headers["Authorization"] == f"Bearer {SECRET}"


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_structured_request_forces_the_function_with_an_inlined_schema() -> None:
    provider, server, _ = provider_for(lambda b: tool_call("classify_academic_intent", {"intent": "attendance_status"}))
    provider.classify_academic_intent("What's my OS attendance?", [])

    body = server.bodies[0]
    assert server.requests[0].url.path.endswith("/chat/completions")
    assert server.requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert body["model"] == DEFAULT_MODEL
    assert body["temperature"] == 0
    assert body["tool_choice"] == {"type": "function", "function": {"name": "classify_academic_intent"}}
    assert body["messages"][0]["role"] == "system" and body["messages"][1]["role"] == "user"
    parameters = body["tools"][0]["function"]["parameters"]
    assert "$ref" not in json.dumps(parameters) and "$defs" not in parameters
    assert "exam_eligibility" in parameters["properties"]["intent"]["enum"]
    assert body["reasoning_effort"] == "low"  # gpt-oss only


def test_non_reasoning_models_get_no_reasoning_parameter() -> None:
    provider, server, _ = provider_for(
        lambda b: tool_call("classify_career_intent", {"intent": "unknown"}), model="llama-3.3-70b-versatile"
    )
    provider.classify_career_intent("hi")
    assert "reasoning_effort" not in server.bodies[0]


def test_inline_refs_resolves_nested_models() -> None:
    from app.llm.providers.anthropic_provider import _PlanProposal

    schema = _inline_refs(_PlanProposal.model_json_schema())
    task = schema["properties"]["tasks"]["items"]
    assert "action_agent" in task["properties"]["agent"]["enum"]
    action = next(option for option in task["properties"]["action"]["anyOf"] if option.get("type") == "object")
    assert "register_event" in action["properties"]["tool_name"]["enum"]


# ---------------------------------------------------------------------------
# Structured outputs validate against the existing Pydantic schemas
# ---------------------------------------------------------------------------


def test_every_intent_classifier_returns_validated_models() -> None:
    answers = {
        "classify_academic_intent": {"intent": "exam_eligibility", "raw_course_reference": "OS"},
        "classify_career_intent": {"intent": "opportunity_discovery"},
        "classify_events_intent": {"intent": "event_discovery"},
        "classify_services_intent": {"intent": "case_status"},
    }
    provider, _, _ = provider_for(lambda b: tool_call(b["tool_choice"]["function"]["name"], answers[b["tool_choice"]["function"]["name"]]))

    academic = provider.classify_academic_intent("My OS attendance is low. Can I write the exam?", check_live_llm.COURSES)
    assert isinstance(academic, AcademicIntentResult)
    assert academic.intent == AcademicIntent.EXAM_ELIGIBILITY and academic.raw_course_reference == "OS"
    assert provider.classify_career_intent("q").intent == CareerIntent.OPPORTUNITY_DISCOVERY
    assert provider.classify_events_intent("q").intent == EventsIntent.EVENT_DISCOVERY
    assert provider.classify_services_intent("q").intent == ServicesIntent.CASE_STATUS


def test_action_plan_becomes_a_valid_dag_with_allowlisted_constraints() -> None:
    provider, server, _ = provider_for(lambda b: tool_call("produce_mission_plan", ACTION_PROPOSAL))
    plan = provider.plan_mission("m-1", "Register me for the Competitive Coding Contest.", supported_agents=ALL_AGENTS)

    assert validate_plan(plan, _registry(), expected_mission_id="m-1").is_valid
    action = plan.tasks[3]
    assert action.agent == AgentName.ACTION_AGENT
    assert action.constraints == {"tool_name": "register_event", "event_title": "Competitive Coding Contest"}
    assert action.dependencies == ["m-1-task-1", "m-1-task-2", "m-1-task-3"]
    assert "- action_agent:" in server.bodies[0]["messages"][1]["content"]


def test_unsupported_goal_yields_zero_tasks_and_an_explanation() -> None:
    provider, _, _ = provider_for(lambda b: tool_call("produce_mission_plan", UNSUPPORTED_PROPOSAL))
    plan = provider.plan_mission("m-2", "Book me a flight to Goa.", supported_agents=ALL_AGENTS)
    assert plan.tasks == [] and plan.unsupported_requests == ["Flight booking is not supported."]


def test_response_generation_is_plain_text_without_tools() -> None:
    provider, server, _ = provider_for(lambda b: text_reply("  You are eligible.  "))
    assert provider._text(system="s", context=_Context()) == "You are eligible."
    assert json.loads(server.bodies[0]["messages"][1]["content"]) == {"x": 1}
    assert "tools" not in server.bodies[0] and "tool_choice" not in server.bodies[0]


# ---------------------------------------------------------------------------
# Malformed output -> LLMProviderError (never a default answer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response, match",
    [
        (tool_call("classify_career_intent", {"intent": "book_flight"}), "failed schema validation"),
        (tool_call("classify_career_intent", "{not json"), "not valid JSON"),
        (tool_call("some_other_tool", {"intent": "unknown"}), "did not include the expected"),
        (text_reply("opportunity_discovery"), "did not include the expected"),
        (tool_call("classify_career_intent", {"intent": "unknown"}, finish_reason="length"), "truncated"),
        ({"choices": []}, "without any choices"),
        (error_body(400, "Failed to call a function.", code="tool_use_failed"), "malformed structured output"),
    ],
)
def test_malformed_structured_output_raises(response: Any, match: str) -> None:
    provider, _, _ = provider_for(lambda b: response)
    with pytest.raises(LLMProviderError, match=match):
        provider.classify_career_intent("Find AI internships.")


def test_empty_text_response_raises() -> None:
    provider, _, _ = provider_for(lambda b: text_reply(None))
    with pytest.raises(LLMProviderError, match="empty response"):
        provider._text(system="s", context=_Context())


def test_plan_with_unknown_agent_fails_validation_instead_of_being_repaired() -> None:
    bad = {"tasks": [{"index": 1, "objective": "Book a flight.", "agent": "travel_agent"}]}
    provider, _, _ = provider_for(lambda b: tool_call("produce_mission_plan", bad))
    with pytest.raises(LLMProviderError, match="failed schema validation"):
        provider.plan_mission("m-3", "Book a flight.", supported_agents=ALL_AGENTS)


# ---------------------------------------------------------------------------
# Transport / HTTP errors
# ---------------------------------------------------------------------------


def test_authentication_error_is_clear_and_never_leaks_the_key() -> None:
    provider, _, sleeps = provider_for(lambda b: error_body(401, f"Invalid API Key {SECRET}", code="invalid_api_key"))
    with pytest.raises(LLMProviderError) as info:
        provider.classify_career_intent("q")
    assert "authentication failed" in str(info.value) and "GROQ_API_KEY" in str(info.value)
    assert SECRET not in str(info.value)
    assert sleeps == []  # auth errors are not retried


def test_model_not_found_names_the_model_and_the_fix() -> None:
    provider, _, _ = provider_for(
        lambda b: error_body(404, "The model `nope` does not exist or you do not have access to it.", code="model_not_found"),
        model="nope",
    )
    with pytest.raises(LLMProviderError, match="'nope' is not available.*CAMPUSNEXUS_LLM_MODEL"):
        provider.classify_career_intent("q")


def test_rate_limit_is_retried_honouring_retry_after_then_succeeds() -> None:
    replies = iter([
        error_body(429, "Rate limit reached", code="rate_limit_exceeded", headers={"retry-after": "3"}),
        tool_call("classify_career_intent", {"intent": "application_status"}),
    ])
    provider, server, sleeps = provider_for(lambda b: next(replies))
    assert provider.classify_career_intent("q").intent == CareerIntent.APPLICATION_STATUS
    assert sleeps == [3.0] and len(server.bodies) == 2


def test_rate_limit_that_persists_raises_after_bounded_retries() -> None:
    provider, server, sleeps = provider_for(
        lambda b: error_body(429, "Rate limit reached", code="rate_limit_exceeded", headers={"retry-after": "999"})
    )
    with pytest.raises(LLMProviderError, match="rate limit exceeded"):
        provider.classify_career_intent("q")
    assert len(server.bodies) == 3  # 1 + DEFAULT_MAX_RETRIES
    assert sleeps == [30.0, 30.0]  # retry-after is capped


def test_timeout_is_retried_then_reported() -> None:
    provider, server, _ = provider_for(lambda b: httpx.ReadTimeout("timed out"))
    with pytest.raises(LLMProviderError, match="timed out") as info:
        provider.classify_career_intent("q")
    assert len(server.bodies) == 3
    assert isinstance(info.value.__cause__, httpx.TimeoutException)


def test_network_error_is_reported() -> None:
    provider, _, _ = provider_for(lambda b: httpx.ConnectError("connection refused"), max_retries=0)
    with pytest.raises(LLMProviderError, match="network error"):
        provider.classify_career_intent("q")


def test_server_error_is_reported_after_retries() -> None:
    provider, server, _ = provider_for(lambda b: error_body(503, "Service unavailable"))
    with pytest.raises(LLMProviderError, match="HTTP 503"):
        provider.classify_career_intent("q")
    assert len(server.bodies) == 3


# ---------------------------------------------------------------------------
# check_live_llm.py --smoke
# ---------------------------------------------------------------------------


def _report() -> Dict[str, Any]:
    return {"checks": [], "smoke": [], "e2e": []}


def test_smoke_runs_exactly_five_requests_and_passes_on_correct_output() -> None:
    provider, server, _ = provider_for(smoke_server)
    report = _report()
    check_live_llm.run_smoke(provider, report)

    assert len(server.bodies) == 5
    assert [c["name"] for c in report["smoke"]] == [name for name, *_ in check_live_llm.SMOKE_CHECKS]
    assert all(c["pass"] for c in report["smoke"]), report["smoke"]
    for check in report["smoke"]:
        assert check["provider"] == "groq" and check["model"] == DEFAULT_MODEL
        assert check["status"] == "schema_valid" and isinstance(check["latency_ms"], int)
    by_name = {c["name"]: c for c in report["smoke"]}
    assert by_name["action_plan"]["agents"] == ["academic_agent", "action_agent", "events_opportunity_agent"]
    assert by_name["unsupported_request"]["agents"] == []


def test_smoke_never_executes_anything() -> None:
    """Smoke mode only calls classify/plan: the only traffic is the five
    structured requests, and no mission/agent/tool is ever constructed."""
    provider, server, _ = provider_for(smoke_server)
    check_live_llm.run_smoke(provider, _report())
    assert {b["tool_choice"]["function"]["name"] for b in server.bodies} == {
        "classify_academic_intent", "classify_career_intent", "produce_mission_plan",
    }


def test_smoke_fails_a_registration_plan_missing_schedule_dependencies() -> None:
    unsafe = json.loads(json.dumps(ACTION_PROPOSAL))
    unsafe["tasks"][3]["depends_on_indices"] = [1]

    def respond(body: Dict[str, Any]) -> Dict[str, Any]:
        if "Competitive Coding Contest" in body["messages"][1]["content"]:
            return tool_call("produce_mission_plan", unsafe)
        return smoke_server(body)

    provider, _, _ = provider_for(respond)
    report = _report()
    check_live_llm.run_smoke(provider, report)
    action = next(c for c in report["smoke"] if c["name"] == "action_plan")
    assert not action["pass"] and action["status"] == "schema_valid, expectation_failed"
    assert any("two academic tasks" in e for e in action["errors"])


def test_smoke_reports_provider_errors_and_wrong_intents_as_failures() -> None:
    def respond(body: Dict[str, Any]) -> Any:
        tool = body["tool_choice"]["function"]["name"]
        if tool == "classify_academic_intent":
            return error_body(401, "Invalid API Key", code="invalid_api_key")
        if tool == "classify_career_intent":
            return tool_call(tool, {"intent": "policy_question"})
        return smoke_server(body)

    provider, _, _ = provider_for(respond)
    report = _report()
    check_live_llm.run_smoke(provider, report)
    by_name = {c["name"]: c for c in report["smoke"]}
    assert by_name["academic_intent"]["status"] == "provider_error" and not by_name["academic_intent"]["pass"]
    assert not by_name["career_intent"]["pass"] and "opportunity_discovery" in by_name["career_intent"]["errors"][0]


def test_smoke_cli_writes_the_report_and_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    provider, _, _ = provider_for(smoke_server)
    requested: List[str] = []
    monkeypatch.setattr(check_live_llm, "get_llm_provider", lambda name: requested.append(name) or provider)
    out = tmp_path / "nested" / "live_smoke_report.json"
    monkeypatch.setattr("sys.argv", ["check_live_llm.py", "--provider", "groq", "--smoke", "--out", str(out)])

    with pytest.raises(SystemExit) as info:
        check_live_llm.main()
    assert info.value.code == 0 and requested == ["groq"]
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["mode"] == "smoke" and report["provider"] == "groq" and len(report["smoke"]) == 5
    assert report["checks"] == []
    assert "5/5 smoke checks passed." in capsys.readouterr().out


def test_smoke_cli_uses_configured_live_provider_and_is_unavailable_without_key(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_LLM_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["check_live_llm.py", "--smoke"])
    with pytest.raises(SystemExit) as info:
        check_live_llm.main()
    assert info.value.code == 2
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_live_check_refuses_a_mock_provider(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(check_live_llm, "get_llm_provider", lambda name: MockLLMProvider())
    monkeypatch.setattr("sys.argv", ["check_live_llm.py", "--smoke"])
    with pytest.raises(SystemExit) as info:
        check_live_llm.main()
    assert info.value.code == 2
    assert "not a real LLM provider" in capsys.readouterr().out
