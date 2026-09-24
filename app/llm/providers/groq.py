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

Phase 12C (free-tier resilience): at most ``CAMPUSNEXUS_LLM_MAX_CONCURRENCY``
(default 1) Groq calls are in flight at once; the Orchestrator's DAG still
schedules independent tasks in parallel, only their HTTP calls queue. A 429
is retried after the wait Groq asks for, and nothing is sent in the meantime.
Rate limits, timeouts, network errors and 5xx that outlast the bounded retries
raise ``LLMTransientError`` (``LLMRateLimitError`` for 429), which the
Orchestrator treats as "provider unavailable", never as a reason to replan.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ValidationError

from app.llm.base import LLMProvider, LLMProviderError, LLMRateLimitError, LLMTransientError
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
    _SYSTEM_ENQUIRY_PROMPT,
    _ENQUIRY_TOOL_NAME,
    _FACULTY_QUERY_TOOL_NAME,
    _HOD_QUERY_TOOL_NAME,
    _SYSTEM_HOD_QUERY_PROMPT,
    _PERMISSION_TOOL_NAME,
    _SYSTEM_FACULTY_QUERY_PROMPT,
    _SYSTEM_PERMISSION_PROMPT,
    _CAREER_INTENT_TOOL_NAME,
    _EVENTS_INTENT_TOOL_NAME,
    _INTENT_TOOL_NAME,
    _SERVICES_INTENT_TOOL_NAME,
    _PlanProposal,
    _proposal_to_plan,
)
from app.schemas.academic import AcademicIntentResult, AcademicResponseContext, CourseSummary
from app.schemas.agent_chat import EnquiryPlan
from app.schemas.career import CareerIntentResult, CareerResponseContext
from app.schemas.enums import AgentName
from app.schemas.events import EventsIntentResult, EventsResponseContext
from app.schemas.faculty import FacultyQueryPlan
from app.schemas.department import HodQueryPlan
from app.schemas.mission import MissionPlan
from app.schemas.services import ServicesIntentResult, ServicesResponseContext
from app.schemas.workflow import PermissionIntent

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
# Upper bound on how long one retry waits for a rate limit to clear, so a
# demo never hangs on a long ``retry-after``.
MAX_RETRY_WAIT_SECONDS = 30.0
# Phase 12C: Groq API calls in flight at once, per provider instance (the app
# builds one). The free tier's tokens-per-minute limit is exhausted by the
# DAG's parallel tasks, so the default is 1. The DAG still schedules tasks in
# parallel; only the HTTP calls queue. Raise it on a higher tier.
DEFAULT_MAX_CONCURRENCY = 1
MAX_CONCURRENCY_ENV = "CAMPUSNEXUS_LLM_MAX_CONCURRENCY"

# Output budgets per operation (Phase 12C review). gpt-oss spends part of
# ``max_completion_tokens`` on reasoning, even at reasoning_effort=low, so each
# budget leaves room for that on top of the output itself:
# - classification returns a two-field schema (tens of tokens);
# - a MissionPlan is a few tasks of JSON (a few hundred tokens);
# - a user-facing answer can list a dozen events.
# A truncated response is still discarded (finish_reason=length), never
# repaired, so these are headroom limits, not squeeze limits.
_CLASSIFY_MAX_TOKENS = 512
_PLAN_MAX_TOKENS = 2048
_RESPONSE_MAX_TOKENS = 1024

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_SERVER_ERROR_STATUS = {500, 502, 503, 504}
# Groq durations look like "6.165s", "1m2.5s" or "250ms".
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_TRY_AGAIN_RE = re.compile(r"try again in ((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", re.IGNORECASE)
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _parse_duration(text: str) -> Optional[float]:
    parts = _DURATION_PART_RE.findall(text or "")
    return sum(float(value) * _DURATION_UNITS[unit] for value, unit in parts) if parts else None


def _max_concurrency_from_env() -> int:
    raw = (os.environ.get(MAX_CONCURRENCY_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise LLMProviderError(f"{MAX_CONCURRENCY_ENV}={raw!r} must be a positive integer.")
    return value


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
        max_concurrency: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``client`` is a test seam: an ``httpx.Client``-like object exposing
        ``post(path, json=...)``. When omitted, a real ``httpx.Client`` is built
        against ``base_url`` with the key in its ``Authorization`` header.

        ``max_concurrency`` bounds Groq calls in flight at once (default:
        ``CAMPUSNEXUS_LLM_MAX_CONCURRENCY``, else 1)."""
        self._model = model or os.environ.get("CAMPUSNEXUS_LLM_MODEL") or DEFAULT_MODEL
        _check_model_compatible(self._model)
        self._max_retries = DEFAULT_MAX_RETRIES if max_retries is None else max_retries
        self._sleep = sleep
        self._clock = clock
        if max_concurrency is not None and max_concurrency < 1:
            raise LLMProviderError(f"max_concurrency must be a positive integer, got {max_concurrency!r}.")
        self._max_concurrency = max_concurrency if max_concurrency is not None else _max_concurrency_from_env()
        self._slots = threading.BoundedSemaphore(self._max_concurrency)
        self._state_lock = threading.Lock()
        # A 429 tells us when the token window reopens; no request (from any
        # thread) is sent before then.
        self._cooldown_until = 0.0
        self._in_flight = 0
        self._stats: Dict[str, Any] = {
            "requests": 0,
            "rate_limit_retries": 0,
            "transient_retries": 0,
            "rate_limit_wait_seconds": 0.0,
            "max_observed_concurrency": 0,
            "max_completion_tokens_used": {},
        }
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

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def stats(self) -> Dict[str, Any]:
        """Counters for live evaluation: requests sent, 429/transient retries,
        seconds waited for rate limits, the most calls ever in flight at once,
        and the largest completion-token count seen per operation."""
        with self._state_lock:
            snapshot = dict(self._stats)
            snapshot["max_completion_tokens_used"] = dict(self._stats["max_completion_tokens_used"])
        snapshot["max_concurrency"] = self._max_concurrency
        return snapshot

    # ------------------------------------------------------------------
    # Request plumbing -- every call goes through _create, so every call gets
    # the same retry/error/finish_reason handling.
    # ------------------------------------------------------------------

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text

    def _fail(self, message: str) -> LLMProviderError:
        return LLMProviderError(self._redact(message))

    def _transient(self, message: str, error_cls: type = LLMTransientError, **details: Any) -> LLMTransientError:
        return error_cls(self._redact(message), provider=self.name, model=self._model, **details)

    def _post(self, body: Dict[str, Any]) -> tuple:
        """POST once per attempt, retrying rate limits, 5xx and transport errors.

        Holds one concurrency slot for the whole call, including retry waits,
        so with the default limit of 1 nothing else is sent while a rate limit
        clears. Returns ``(response, retries_used)``.
        """
        with self._slots:
            attempt = 0
            while True:
                self._wait_for_cooldown()
                try:
                    response = self._send(body)
                except self._httpx.TimeoutException as exc:
                    if attempt < self._max_retries:
                        attempt += 1
                        self._count_retry("transient_retries")
                        self._sleep(float(attempt))
                        continue
                    raise self._transient(
                        f"Groq API request timed out ({type(exc).__name__}).", kind="timeout", retries=attempt
                    ) from exc
                except self._httpx.HTTPError as exc:
                    if attempt < self._max_retries:
                        attempt += 1
                        self._count_retry("transient_retries")
                        self._sleep(float(attempt))
                        continue
                    raise self._transient(
                        f"Groq API network error ({type(exc).__name__}): {exc}", kind="network", retries=attempt
                    ) from exc

                if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                    attempt += 1
                    wait = self._retry_wait(response, attempt)
                    if response.status_code == 429:
                        self._wait_out_rate_limit(wait)
                    else:
                        self._count_retry("transient_retries")
                        self._sleep(wait)
                    continue
                return response, attempt

    def _send(self, body: Dict[str, Any]) -> Any:
        with self._state_lock:
            self._in_flight += 1
            self._stats["requests"] += 1
            self._stats["max_observed_concurrency"] = max(self._stats["max_observed_concurrency"], self._in_flight)
        try:
            return self._client.post("/chat/completions", json=body)
        finally:
            with self._state_lock:
                self._in_flight -= 1

    def _count_retry(self, counter: str) -> None:
        with self._state_lock:
            self._stats[counter] += 1

    def _wait_for_cooldown(self) -> None:
        with self._state_lock:
            remaining = self._cooldown_until - self._clock()
        if remaining > 0:
            self._sleep(remaining)

    def _wait_out_rate_limit(self, wait: float) -> None:
        """Block every request until the provider's window reopens, then retry."""
        with self._state_lock:
            until = self._clock() + wait
            self._cooldown_until = max(self._cooldown_until, until)
            self._stats["rate_limit_retries"] += 1
            self._stats["rate_limit_wait_seconds"] = round(self._stats["rate_limit_wait_seconds"] + wait, 3)
        self._sleep(wait)
        with self._state_lock:
            if self._cooldown_until <= until:
                self._cooldown_until = 0.0  # this wait is over; don't make the retry wait it again

    @staticmethod
    def _retry_after_seconds(response: Any) -> Optional[float]:
        """Seconds until Groq accepts another request, from the most specific
        signal available: the ``retry-after`` header, then the token-window
        reset header, then the "try again in 6.165s" text in the error body
        (Groq exposes no other structured field; this parsing stays here)."""
        headers = getattr(response, "headers", None) or {}
        try:
            return float(headers.get("retry-after"))
        except (TypeError, ValueError):
            pass
        reset = _parse_duration(headers.get("x-ratelimit-reset-tokens") or "")
        if reset is not None:
            return reset
        try:
            message = (response.json().get("error") or {}).get("message") or ""
        except (ValueError, AttributeError):
            return None
        match = _TRY_AGAIN_RE.search(message)
        return _parse_duration(match.group(1)) if match else None

    @classmethod
    def _retry_wait(cls, response: Any, attempt: int) -> float:
        wait = cls._retry_after_seconds(response)
        return max(0.0, min(float(attempt) if wait is None else wait, MAX_RETRY_WAIT_SECONDS))

    def _raise_for_status(self, response: Any, retries: int = 0) -> None:
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
            raise self._transient(
                f"Groq rate limit exceeded (HTTP 429) after {retries} retries: {detail}",
                LLMRateLimitError, status_code=429, retry_after_seconds=self._retry_after_seconds(response), retries=retries,
            )
        if status in _SERVER_ERROR_STATUS:
            raise self._transient(
                f"Groq API call failed (HTTP {status}{', ' + code if code else ''}) after {retries} retries: {detail}",
                kind="server_error", status_code=status, retries=retries,
            )
        if status == 404 or code == "model_not_found" or code == "model_decommissioned":
            raise self._fail(
                f"Groq model {self._model!r} is not available to this account (HTTP {status}, {code or 'not found'}): "
                f"{detail} Set CAMPUSNEXUS_LLM_MODEL to a model listed at https://console.groq.com/docs/models."
            )
        if code == "tool_use_failed":
            raise self._fail(f"Groq model produced malformed structured output (tool_use_failed): {detail}")
        raise self._fail(f"Groq API call failed (HTTP {status}{', ' + code if code else ''}): {detail}")

    def _create(
        self, *, max_tokens: int, system: str, content: str, tool: Optional[Dict[str, Any]] = None, operation: str = "response"
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

        response, retries = self._post(body)
        self._raise_for_status(response, retries)
        try:
            payload = response.json()
            choice = payload["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise self._fail("Groq returned a response without any choices.") from exc
        self._record_usage(operation, payload.get("usage"))
        if choice.get("finish_reason") == "length":
            raise self._fail(f"Groq response was truncated at max_completion_tokens={max_tokens}; output discarded.")
        return choice.get("message") or {}

    def _record_usage(self, operation: str, usage: Any) -> None:
        """Largest completion (reasoning + output) seen per operation, so the
        budgets above can be checked against real traffic."""
        tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if isinstance(tokens, int):
            with self._state_lock:
                seen = self._stats["max_completion_tokens_used"]
                seen[operation] = max(seen.get(operation, 0), tokens)

    def _structured(self, *, max_tokens: int, system: str, content: str, tool_name: str, description: str, schema_cls: type):
        message = self._create(
            max_tokens=max_tokens,
            system=system,
            content=content,
            tool={"name": tool_name, "description": description, "parameters": _inline_refs(schema_cls.model_json_schema())},
            operation=tool_name,
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

    def plan_enquiry(self, query: str) -> EnquiryPlan:
        # Same forced-function-call path, concurrency limit and retry handling as every other call.
        return self._structured(
            max_tokens=_PLAN_MAX_TOKENS, system=_SYSTEM_ENQUIRY_PROMPT, content=f"Student question: {query}",
            tool_name=_ENQUIRY_TOOL_NAME, description="Record which specialists to consult.", schema_cls=EnquiryPlan,
        )

    def plan_permission_request(self, message: str) -> PermissionIntent:
        return self._structured(
            max_tokens=_CLASSIFY_MAX_TOKENS, system=_SYSTEM_PERMISSION_PROMPT, content=f"Student message: {message}",
            tool_name=_PERMISSION_TOOL_NAME, description="Record the interpreted request.", schema_cls=PermissionIntent,
        )

    def plan_faculty_query(self, message: str) -> FacultyQueryPlan:
        return self._structured(
            max_tokens=_CLASSIFY_MAX_TOKENS, system=_SYSTEM_FACULTY_QUERY_PROMPT, content=f"Faculty question: {message}",
            tool_name=_FACULTY_QUERY_TOOL_NAME, description="Record the classified question.", schema_cls=FacultyQueryPlan,
        )

    def plan_hod_query(self, message: str) -> HodQueryPlan:
        return self._structured(
            max_tokens=_CLASSIFY_MAX_TOKENS, system=_SYSTEM_HOD_QUERY_PROMPT, content=f"HOD question: {message}",
            tool_name=_HOD_QUERY_TOOL_NAME, description="Record the classified question.", schema_cls=HodQueryPlan,
        )

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
