"""Offline tests for the OpenRouter provider (CAMPUS AI).

No network and no real key: a genuine ``httpx.Client`` with an ``httpx.MockTransport`` answers with scripted
OpenAI-compatible responses, so the request body (model chain, failover, bounded context), status handling and
response validation are all exercised.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

import pytest

httpx = pytest.importorskip("httpx")

from app.agentos.providers import OpenRouterAgentBrain, build_agent_brain
from app.llm.base import LLMMalformedOutputError, LLMProviderError, LLMTransientError
from app.llm.factory import get_llm_provider
from app.llm.providers.openrouter import (
    DEFAULT_FALLBACK_MODELS, DEFAULT_PRIMARY_MODEL, MAX_MODELS, OpenRouterLLMProvider, model_chain,
)
from app.llm.router import ai_context, routed
from app.schemas.academic import AcademicIntent, AcademicIntentResult
from app.schemas.enums import UserRole
from app.schemas.grounded import GroundedPrompt
from app.services.grounded_answers import build_prompt, classify, prompt_tokens
from app.services.grounded_facts import FactResult, GroundedIdentity

SECRET = "sk-or-v1-test-secret-key-value"
ANSWERED_BY = "nvidia/nemotron-3.5-lightning:free"


def completion(content: Any, model: str = ANSWERED_BY, finish_reason: str = "stop") -> Dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content)
    return {"model": model, "usage": {"prompt_tokens": 321, "completion_tokens": 45},
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": text}}]}


class Server:
    def __init__(self, respond: Callable[[Dict[str, Any]], Any]) -> None:
        self.respond, self.bodies, self.requests = respond, [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(request)
        self.bodies.append(body)
        result = self.respond(body)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)


def provider_for(respond, **kwargs) -> tuple:
    server = Server(respond)
    client = httpx.Client(base_url="https://openrouter.test/api/v1", headers={"Authorization": f"Bearer {SECRET}"},
                          transport=httpx.MockTransport(server))
    return OpenRouterLLMProvider(api_key=SECRET, client=client, sleep=lambda _s: None, **kwargs), server


def grounded_prompt() -> GroundedPrompt:
    identity = GroundedIdentity(1, 1, UserRole.STUDENT, "Aditi Rao", student_code="STU-DEMO-001")
    result = FactResult(facts=[("Next exam", "Quiz 2: Transport Layer, Sat 10 Oct 16:00")], found=True,
                        draft="Your next exam is Quiz 2: Transport Layer, Sat 10 Oct 16:00.")
    return build_prompt(classify("When is my next exam?", UserRole.STUDENT), identity, "When is my next exam?", result)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("CAMPUSNEXUS_OPENROUTER_MODEL", "CAMPUSNEXUS_OPENROUTER_FALLBACK_MODELS", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# --- Configuration ---------------------------------------------------------------------------------------------


def test_default_fallback_chain_is_the_preferred_models() -> None:
    assert model_chain() == [DEFAULT_PRIMARY_MODEL, *DEFAULT_FALLBACK_MODELS] == [
        "google/gemma-4-26b-a4b-it:free", "nvidia/nemotron-3.5-lightning:free", "openrouter/free"]


def test_model_ids_come_from_the_environment_and_stay_bounded(monkeypatch) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_OPENROUTER_MODEL", "a/one:free")
    monkeypatch.setenv("CAMPUSNEXUS_OPENROUTER_FALLBACK_MODELS", "b/two:free, a/one:free, c/three, d/four")
    assert model_chain() == ["a/one:free", "b/two:free", "c/three"] and len(model_chain()) == MAX_MODELS
    monkeypatch.setenv("CAMPUSNEXUS_OPENROUTER_FALLBACK_MODELS", "bad model; rm -rf")
    with pytest.raises(LLMProviderError):
        model_chain()


def test_the_request_uses_openrouter_fallbacks_with_provider_failover() -> None:
    provider, server = provider_for(lambda body: completion({"answer": "Quiz 2: Transport Layer, Sat 10 Oct 16:00."}))
    provider.synthesize_grounded_answer(grounded_prompt())
    body = server.bodies[0]
    assert body["model"] == DEFAULT_PRIMARY_MODEL and body["models"] == model_chain()
    assert body["provider"] == {"allow_fallbacks": True} and body["reasoning"] == {"exclude": True}
    assert len(server.requests) == 1  # one bounded request; fallbacks are OpenRouter's, not an application loop


def test_a_missing_key_is_refused_without_a_request() -> None:
    with pytest.raises(LLMProviderError, match="OPENROUTER_API_KEY"):
        OpenRouterLLMProvider()


def test_the_factory_builds_openrouter(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    provider = get_llm_provider("openrouter")
    assert isinstance(provider, OpenRouterLLMProvider) and provider.is_live and SECRET not in repr(provider)


# --- Bounded context -------------------------------------------------------------------------------------------


def test_the_request_contains_only_the_bounded_grounded_context() -> None:
    provider, server = provider_for(lambda body: completion({"answer": "Quiz 2: Transport Layer, Sat 10 Oct 16:00."}))
    prompt = grounded_prompt()
    provider.synthesize_grounded_answer(prompt)
    messages = server.bodies[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    sent = json.loads(messages[1]["content"])
    assert set(sent) <= {"intent", "caller_role", "caller_name", "question", "facts", "sources",
                         "recent_conversation", "draft_answer"}
    assert "STU-DEMO-001" not in messages[1]["content"]  # identity reaches the model as role + display name only
    assert prompt_tokens(prompt) < 1000 and len(json.dumps(server.bodies[0])) < 16000


# --- Response validation ---------------------------------------------------------------------------------------


def test_the_model_that_actually_answered_is_recorded_in_telemetry() -> None:
    provider, _ = provider_for(lambda body: completion({"answer": "Quiz 2: Transport Layer, Sat 10 Oct 16:00."}))
    routed_provider = routed(provider)
    with ai_context(organization_id=None) as context:
        synthesis = routed_provider.synthesize_grounded_answer(grounded_prompt())
    assert synthesis.answer.startswith("Quiz 2")
    record = context.records[-1]
    assert (record.operation, record.provider, record.model) == ("synthesize_grounded_answer", "openrouter", ANSWERED_BY)
    assert (record.input_tokens, record.output_tokens, record.success) == (321, 45, True)


@pytest.mark.parametrize("reply", [
    completion("Sure! Your next exam is Quiz 2."),                     # prose, not JSON
    completion({"answer": ""}),                                         # fails the schema
    completion({"answer": "ok", "reasoning": "secret thoughts"}),       # unexpected field
    completion({"answer": "ok"}, finish_reason="error"),                # a completion that errored
    {"error": {"code": 400, "message": "Provider returned error"}},     # a 200 carrying an error
    {"model": ANSWERED_BY, "choices": []},                              # no choices
])
def test_malformed_200_responses_are_never_trusted(reply) -> None:
    provider, _ = provider_for(lambda body: reply)
    with pytest.raises(LLMProviderError):
        provider.synthesize_grounded_answer(grounded_prompt())


def test_truncated_output_is_discarded() -> None:
    provider, _ = provider_for(lambda body: completion({"answer": "Quiz"}, finish_reason="length"))
    with pytest.raises(LLMMalformedOutputError):
        provider.synthesize_grounded_answer(grounded_prompt())


def test_structured_intents_use_validated_json_not_tool_calls() -> None:
    provider, server = provider_for(lambda body: completion({"intent": "attendance_status", "raw_course_reference": "OS"}))
    result = provider.classify_academic_intent("What's my OS attendance?", [])
    assert isinstance(result, AcademicIntentResult) and result.intent == AcademicIntent.ATTENDANCE_STATUS
    assert "tools" not in server.bodies[0] and server.bodies[0]["response_format"]["type"] == "json_schema"


# --- Failures and the key -----------------------------------------------------------------------------------------


def test_transport_failures_are_transient_and_bounded() -> None:
    provider, server = provider_for(lambda body: httpx.Response(503, json={"error": {"message": "down"}}))
    with pytest.raises(LLMTransientError):
        provider.synthesize_grounded_answer(grounded_prompt())
    assert len(server.requests) == 2  # the first attempt plus one bounded retry


def test_the_key_never_reaches_errors_or_logs(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    provider, _ = provider_for(lambda body: httpx.Response(401, json={"error": {"message": f"bad key {SECRET}"}}))
    with pytest.raises(LLMProviderError) as raised:
        provider.synthesize_grounded_answer(grounded_prompt())
    assert SECRET not in str(raised.value) and SECRET not in repr(provider)
    assert SECRET not in caplog.text


def test_the_key_is_never_returned_by_the_api(monkeypatch, api_client) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    for path in ("/health", "/openapi.json"):
        response = api_client.get(path)
        assert SECRET not in response.text


def test_openrouter_can_be_the_agentos_brain_without_a_fallback(monkeypatch) -> None:
    assert build_agent_brain("openrouter").code == "PROVIDER_NOT_CONFIGURED"  # no key: refused, never another brain
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    brain = build_agent_brain("openrouter")
    assert isinstance(brain, OpenRouterAgentBrain) and brain.provider_name == "openrouter"
