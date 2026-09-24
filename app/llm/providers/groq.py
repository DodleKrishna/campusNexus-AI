"""Groq-backed LLMProvider (Phase 12) -- a second real runtime provider.

Groq serves an OpenAI-compatible Chat Completions API, so this provider talks
to it with ``httpx`` (already installed via chromadb and the ``app`` extra)
instead of adding a vendor SDK. ``httpx`` is imported lazily, mirroring the
Anthropic provider, so the offline/mock path never needs it.

Structured output uses a *forced function call* whose ``parameters`` schema is
built from the same Pydantic models the Anthropic provider uses. Groq checks
the call's arguments against that schema server-side (an unparseable call
comes back as a 400 ``tool_use_failed``); the arguments are then
``json.loads``-ed and Pydantic-validated here -- never regex-parsed from prose
(CLAUDE.md Structured Output Requirement). Forced function calling is used
rather than ``response_format=json_schema`` because it works across Groq's
tool-capable models, whereas schema-constrained ``response_format`` is limited
to a few model families.

Prompts, the agent capability descriptions, the plan proposal schema and the
proposal -> ``MissionPlan`` conversion are imported from the Anthropic
provider unchanged, so both real providers plan under identical rules.

Every failure -- missing key, auth, rate limit, unknown model, timeout/network
error, truncated or malformed output, schema-validation failure -- raises
``LLMProviderError`` with the API key redacted. Nothing here ever falls back
to the mock provider.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ValidationError

from app.llm.base import LLMProvider, LLMProviderError
from app.llm.providers.anthropic_provider import (
    AGENT_CAPABILITIES,
    _PLAN_TOOL_NAME,
    _SYSTEM_CAREER_INTENT_PROMPT,
    _SYSTEM_CAREER_RESPONSE_PROMPT,
    _SYSTEM_EVENTS_INTENT_PROMPT,
    _SYSTEM_EVENTS_RESPONSE_PROMPT,
    _SYSTEM_INTENT_PROMPT,
    _SYSTEM_PLAN_PROMPT,
    _SYSTEM_RESPONSE_PROMPT,
    _SYSTEM_SERVICES_INTENT_PROMPT,
    _SYSTEM_SERVICES_RESPONSE_PROMPT,
    _CAREER_INTENT_TOOL_NAME,
    _EVENTS_INTENT_TOOL_NAME,
    _INTENT_TOOL_NAME,
    _SERVICES_INTENT_TOOL_NAME,
    _PlanProposal,
    _proposal_to_plan,
)
from app.schemas.academic import AcademicIntentResult, AcademicResponseContext, CourseSummary
from app.schemas.career import CareerIntentResult, CareerResponseContext
from app.schemas.enums import AgentName
from app.schemas.events import EventsIntentResult, EventsResponseContext
from app.schemas.mission import MissionPlan
from app.schemas.services import ServicesIntentResult, ServicesResponseContext

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
# Upper bound on how long one retry waits for a rate limit to clear, so a
# demo never hangs on a long ``retry-after``.
MAX_RETRY_WAIT_SECONDS = 30.0

# Groq counts ``max_completion_tokens`` against the per-minute token limit, so
# budgets stay modest; reasoning models spend part of it on reasoning.
_CLASSIFY_MAX_TOKENS = 1024
_PLAN_MAX_TOKENS = 4096
_RESPONSE_MAX_TOKENS = 1024

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _inline_refs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``schema`` with every local ``#/$defs/...`` reference inlined.

    Pydantic emits ``$defs``/``$ref`` for nested models and enums; inlining
    them gives every OpenAI-compatible backend a self-contained schema.
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                resolved = dict(resolve(defs[ref.split("/")[-1]]))
                resolved.update({k: resolve(v) for k, v in node.items() if k != "$ref"})
                return resolved
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


def _check_model_compatible(model: str) -> None:
    # CAMPUSNEXUS_LLM_MODEL is shared between providers; a Claude id left over
    # from Anthropic mode would otherwise surface as a confusing 404 mid-demo.
    if model.startswith("claude-"):
        raise LLMProviderError(
            f"CAMPUSNEXUS_LLM_MODEL={model!r} is an Anthropic model id, but CAMPUSNEXUS_LLM_PROVIDER=groq. "
            f"Set CAMPUSNEXUS_LLM_MODEL to a Groq model id (default: {DEFAULT_MODEL}) or leave it empty."
        )


class GroqLLMProvider(LLMProvider):
    """Real LLM provider (Groq, OpenAI-compatible API) behind the LLMProvider abstraction."""

    name = "groq"
    is_live = True

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        base_url: Optional[str] = None,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """``client`` is a test seam: an ``httpx.Client``-like object exposing
        ``post(path, json=...)``. When omitted, a real ``httpx.Client`` is built
        against ``base_url`` with the key in its ``Authorization`` header."""
        self._model = model or os.environ.get("CAMPUSNEXUS_LLM_MODEL") or DEFAULT_MODEL
        _check_model_compatible(self._model)
        self._max_retries = DEFAULT_MAX_RETRIES if max_retries is None else max_retries
        self._sleep = sleep
        self._api_key = api_key or os.environ.get("GROQ_API_KEY") or ""

        try:
            import httpx  # lazy import: only required when this provider is selected
        except ImportError as exc:
            raise LLMProviderError(
                "CAMPUSNEXUS_LLM_PROVIDER=groq requires the 'httpx' package. "
                'Install it with: pip install -e ".[dev,llm]"'
            ) from exc
        self._httpx = httpx

        if client is not None:
            self._client = client
            return

        if not self._api_key:
            raise LLMProviderError(
                "CAMPUSNEXUS_LLM_PROVIDER=groq requires GROQ_API_KEY to be set (see .env.example). "
                "Unset CAMPUSNEXUS_LLM_PROVIDER (or set it to 'mock') to run the offline demo instead."
            )
        resolved_timeout = timeout if timeout is not None else float(
            os.environ.get("CAMPUSNEXUS_LLM_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS
        )
        self._client = httpx.Client(
            base_url=base_url or DEFAULT_BASE_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=resolved_timeout,
        )

    @property
    def model_name(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    # Request plumbing -- every call goes through _create, so every call gets
    # the same retry/error/finish_reason handling.
    # ------------------------------------------------------------------

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text

    def _fail(self, message: str) -> LLMProviderError:
        return LLMProviderError(self._redact(message))

    def _post(self, body: Dict[str, Any]) -> Any:
        """POST once per attempt, retrying rate limits, 5xx and transport errors."""
        attempt = 0
        while True:
            try:
                response = self._client.post("/chat/completions", json=body)
            except self._httpx.TimeoutException as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    self._sleep(float(attempt))
                    continue
                raise self._fail(f"Groq API request timed out ({type(exc).__name__}).") from exc
            except self._httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    self._sleep(float(attempt))
                    continue
                raise self._fail(f"Groq API network error ({type(exc).__name__}): {exc}") from exc

            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                attempt += 1
                self._sleep(self._retry_wait(response, attempt))
                continue
            return response

    @staticmethod
    def _retry_wait(response: Any, attempt: int) -> float:
        try:
            wait = float(response.headers.get("retry-after", attempt))
        except (TypeError, ValueError):
            wait = float(attempt)
        return max(0.0, min(wait, MAX_RETRY_WAIT_SECONDS))

    def _raise_for_status(self, response: Any) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            error = response.json().get("error") or {}
        except (ValueError, AttributeError):
            error = {}
        code = error.get("code") or error.get("type") or ""
        detail = error.get("message") or (response.text or "")[:300]
        if status in (401, 403):
            raise self._fail(f"Groq authentication failed (HTTP {status}): check GROQ_API_KEY. {detail}")
        if status == 429:
            raise self._fail(f"Groq rate limit exceeded (HTTP 429) after {self._max_retries} retries: {detail}")
        if status == 404 or code == "model_not_found" or code == "model_decommissioned":
            raise self._fail(
                f"Groq model {self._model!r} is not available to this account (HTTP {status}, {code or 'not found'}): "
                f"{detail} Set CAMPUSNEXUS_LLM_MODEL to a model listed at https://console.groq.com/docs/models."
            )
        if code == "tool_use_failed":
            raise self._fail(f"Groq model produced malformed structured output (tool_use_failed): {detail}")
        raise self._fail(f"Groq API call failed (HTTP {status}{', ' + code if code else ''}): {detail}")

    def _create(
        self, *, max_tokens: int, system: str, content: str, tool: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        }
        if tool is not None:
            body["tools"] = [{"type": "function", "function": tool}]
            body["tool_choice"] = {"type": "function", "function": {"name": tool["name"]}}
        if self._model.startswith("openai/gpt-oss"):
            # Reasoning tokens share max_completion_tokens; keep them small.
            body["reasoning_effort"] = "low"

        response = self._post(body)
        self._raise_for_status(response)
        try:
            payload = response.json()
            choice = payload["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise self._fail("Groq returned a response without any choices.") from exc
        if choice.get("finish_reason") == "length":
            raise self._fail(f"Groq response was truncated at max_completion_tokens={max_tokens}; output discarded.")
        return choice.get("message") or {}

    def _structured(self, *, max_tokens: int, system: str, content: str, tool_name: str, description: str, schema_cls: type):
        message = self._create(
            max_tokens=max_tokens,
            system=system,
            content=content,
            tool={"name": tool_name, "description": description, "parameters": _inline_refs(schema_cls.model_json_schema())},
        )
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != tool_name:
                continue
            try:
                arguments = json.loads(function.get("arguments") or "")
            except (TypeError, ValueError) as exc:
                raise self._fail(f"Groq '{tool_name}' arguments were not valid JSON: {exc}") from exc
            try:
                return schema_cls.model_validate(arguments)
            except ValidationError as exc:
                raise self._fail(f"Groq '{tool_name}' output failed schema validation: {exc}") from exc
        raise self._fail(f"Groq response did not include the expected '{tool_name}' function call.")

    def _text(self, *, system: str, context: BaseModel) -> str:
        message = self._create(
            max_tokens=_RESPONSE_MAX_TOKENS,
            system=system,
            content=json.dumps(context.model_dump(mode="json")),
        )
        text = (message.get("content") or "").strip()
        if not text:
            raise self._fail("Groq returned an empty response.")
        return text

    # ------------------------------------------------------------------
    # Academic Agent
    # ------------------------------------------------------------------

    def classify_academic_intent(
        self, query: str, enrolled_courses: List[CourseSummary]
    ) -> AcademicIntentResult:
        course_lines = "\n".join(f"- {c.course_code}: {c.title}" for c in enrolled_courses) or "(none)"
        return self._structured(
            max_tokens=_CLASSIFY_MAX_TOKENS,
            system=_SYSTEM_INTENT_PROMPT,
            content=f"Student's enrolled courses:\n{course_lines}\n\nQuery: {query}",
            tool_name=_INTENT_TOOL_NAME,
            description="Record the classified academic intent.",
            schema_cls=AcademicIntentResult,
        )

    def generate_academic_response(self, context: AcademicResponseContext) -> str:
        return self._text(system=_SYSTEM_RESPONSE_PROMPT, context=context)

    # ------------------------------------------------------------------
    # Mission Orchestrator
    # ------------------------------------------------------------------

    def plan_mission(
        self, mission_id: str, goal: str, *, supported_agents: List[AgentName]
    ) -> MissionPlan:
        capability_lines = "\n".join(
            f"- {agent.value}: {AGENT_CAPABILITIES.get(agent, 'No description available.')}" for agent in supported_agents
        )
        proposal = self._structured(
            max_tokens=_PLAN_MAX_TOKENS,
            system=_SYSTEM_PLAN_PROMPT,
            content=f"Supported agents:\n{capability_lines}\n\nStudent goal: {goal}",
            tool_name=_PLAN_TOOL_NAME,
            description="Record the decomposed mission plan.",
            schema_cls=_PlanProposal,
        )
        try:
            return _proposal_to_plan(mission_id, goal, proposal)
        except (ValidationError, ValueError) as exc:
            raise self._fail(f"Groq produced a structurally invalid mission plan: {exc}") from exc

    # ------------------------------------------------------------------
    # Specialist agents
    # ------------------------------------------------------------------

    def classify_career_intent(self, query: str) -> CareerIntentResult:
        return self._structured(
            max_tokens=_CLASSIFY_MAX_TOKENS, system=_SYSTEM_CAREER_INTENT_PROMPT, content=f"Query: {query}",
            tool_name=_CAREER_INTENT_TOOL_NAME, description="Record the classified intent.", schema_cls=CareerIntentResult,
        )

    def generate_career_response(self, context: CareerResponseContext) -> str:
        return self._text(system=_SYSTEM_CAREER_RESPONSE_PROMPT, context=context)

    def classify_events_intent(self, query: str) -> EventsIntentResult:
        return self._structured(
            max_tokens=_CLASSIFY_MAX_TOKENS, system=_SYSTEM_EVENTS_INTENT_PROMPT, content=f"Query: {query}",
            tool_name=_EVENTS_INTENT_TOOL_NAME, description="Record the classified intent.", schema_cls=EventsIntentResult,
        )

    def generate_events_response(self, context: EventsResponseContext) -> str:
        return self._text(system=_SYSTEM_EVENTS_RESPONSE_PROMPT, context=context)

    def classify_services_intent(self, query: str) -> ServicesIntentResult:
        return self._structured(
            max_tokens=_CLASSIFY_MAX_TOKENS, system=_SYSTEM_SERVICES_INTENT_PROMPT, content=f"Query: {query}",
            tool_name=_SERVICES_INTENT_TOOL_NAME, description="Record the classified intent.", schema_cls=ServicesIntentResult,
        )

    def generate_services_response(self, context: ServicesResponseContext) -> str:
        return self._text(system=_SYSTEM_SERVICES_RESPONSE_PROMPT, context=context)
