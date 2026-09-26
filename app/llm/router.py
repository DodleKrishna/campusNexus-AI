"""Adaptive intelligence router (hackathon fast-finish of Phase 23/24).

Every AI operation is routed BEFORE it runs:

    NO_AI     deterministic DB / rules / workflow logic (recorded, never calls a model)
    LIGHT     openai/gpt-oss-20b  -- intent classification, extraction, simple responses
    ADVANCED  openai/gpt-oss-120b -- multi-agent mission planning / cross-domain synthesis

``RoutedLLMProvider`` wraps the existing providers without changing any agent:
each ``LLMProvider`` method is one operation with a fixed level. A failed call
is never retried on a cheaper model -- provider errors propagate unchanged, and
an exhausted organization budget raises ``AIBudgetExceededError``
(``AI_BUDGET_EXCEEDED``) before any model is called. Deterministic features
never go through this class, so they keep working without AI.

Telemetry (organization, level, model, tokens when the provider reports them,
latency, success, estimated cost) is buffered per ``ai_context`` and written by
``app.services.ai_usage`` when the context ends.
"""
from __future__ import annotations

import contextvars
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from pydantic import BaseModel, ConfigDict

from app.llm.base import LLMProvider, LLMProviderError
from app.schemas.enums import IntelligenceLevel

LIGHT_MODEL = os.environ.get("CAMPUSNEXUS_LLM_LIGHT_MODEL") or "openai/gpt-oss-20b"
ADVANCED_MODEL = os.environ.get("CAMPUSNEXUS_LLM_ADVANCED_MODEL") or "openai/gpt-oss-120b"
AI_BUDGET_EXCEEDED = "AI_BUDGET_EXCEEDED"

# Estimated USD per 1M tokens (input, output). Estimates for the dashboard only -- not billing.
PRICES_PER_MILLION: Dict[str, tuple] = {
    "openai/gpt-oss-20b": (0.075, 0.30),
    "openai/gpt-oss-120b": (0.15, 0.60),
}

# The level of each AI operation, fixed before execution (the "why" is shown on the Control Tower).
OPERATION_LEVELS: Dict[str, tuple] = {
    "plan_mission": (IntelligenceLevel.ADVANCED, "multi-agent mission planning across specialist agents"),
    "classify_academic_intent": (IntelligenceLevel.LIGHT, "intent classification"),
    "classify_career_intent": (IntelligenceLevel.LIGHT, "intent classification"),
    "classify_events_intent": (IntelligenceLevel.LIGHT, "intent classification"),
    "classify_services_intent": (IntelligenceLevel.LIGHT, "intent classification"),
    "plan_enquiry": (IntelligenceLevel.LIGHT, "simple question routing"),
    "plan_permission_request": (IntelligenceLevel.LIGHT, "request extraction"),
    "plan_faculty_query": (IntelligenceLevel.LIGHT, "simple question routing"),
    "plan_hod_query": (IntelligenceLevel.LIGHT, "simple question routing"),
    "plan_admin_query": (IntelligenceLevel.LIGHT, "simple question routing"),
    "generate_academic_response": (IntelligenceLevel.LIGHT, "explaining a deterministic result"),
    "generate_career_response": (IntelligenceLevel.LIGHT, "explaining a deterministic result"),
    "generate_events_response": (IntelligenceLevel.LIGHT, "explaining a deterministic result"),
    "generate_services_response": (IntelligenceLevel.LIGHT, "explaining a deterministic result"),
}


class RoutingDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: str
    level: IntelligenceLevel
    model: Optional[str]
    reason: str


class AIBudgetExceededError(LLMProviderError):
    """The organization's monthly AI budget is used up. AI-required work stops; nothing is fabricated."""

    code = AI_BUDGET_EXCEEDED

    def __init__(self, organization_id: int, budget: float, spent: float) -> None:
        super().__init__(f"{AI_BUDGET_EXCEEDED}: the organization's monthly AI budget "
                         f"(${budget:.2f}) is used up (${spent:.4f} spent). AI features are paused.")
        self.organization_id, self.budget, self.spent = organization_id, budget, spent


def route(operation: str, override: Optional[IntelligenceLevel] = None) -> RoutingDecision:
    """The level/model for ``operation``; a deployed agent's configured LIGHT/ADVANCED level overrides the default."""
    level, reason = OPERATION_LEVELS.get(operation, (IntelligenceLevel.LIGHT, "default for an unlisted AI operation"))
    if override in (IntelligenceLevel.LIGHT, IntelligenceLevel.ADVANCED):
        level, reason = override, f"agent deployment configured {override.value}"
    return RoutingDecision(operation=operation, level=level, reason=reason,
                           model=ADVANCED_MODEL if level == IntelligenceLevel.ADVANCED else LIGHT_MODEL)


def no_ai(operation: str, reason: str) -> RoutingDecision:
    return RoutingDecision(operation=operation, level=IntelligenceLevel.NO_AI, model=None, reason=reason)


def estimate_cost(model: Optional[str], input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    price = PRICES_PER_MILLION.get(model or "")
    if price is None or input_tokens is None or output_tokens is None:
        return None  # unavailable -- never invented
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 8)


# ---------------------------------------------------------------------------------------------------------
# Context: which organization / mission the AI work belongs to (buffered telemetry)
# ---------------------------------------------------------------------------------------------------------


@dataclass
class UsageRecord:
    operation: str
    level: IntelligenceLevel
    model: Optional[str]
    provider: str
    mission_id: Optional[str]
    agent_key: Optional[str]
    run_id: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    latency_ms: int
    success: bool
    error_kind: Optional[str]
    estimated_cost_usd: Optional[float]
    created_at: datetime


@dataclass
class AIContext:
    organization_id: Optional[int]
    mission_id: Optional[str] = None
    agent_key: Optional[str] = None  # the deployed product agent serving this run
    run_id: Optional[str] = None
    level_override: Optional[IntelligenceLevel] = None  # from the agent deployment
    records: List[UsageRecord] = field(default_factory=list)


_CURRENT: contextvars.ContextVar[Optional[AIContext]] = contextvars.ContextVar("campusnexus_ai_context", default=None)


def current_ai_context() -> Optional[AIContext]:
    return _CURRENT.get()


@contextmanager
def ai_context(organization_id: Optional[int], mission_id: Optional[str] = None, *, recorder: Any = None,
               agent_key: Optional[str] = None, level_override: Optional[IntelligenceLevel] = None) -> Iterator[AIContext]:
    """Attribute the AI calls inside the block to an organization (and mission / deployed agent); persist on exit."""
    parent = _CURRENT.get()
    if (parent is not None and parent.organization_id == organization_id and mission_id in (None, parent.mission_id)
            and agent_key in (None, parent.agent_key)):
        yield parent  # nested: the outer context owns the records
        return
    context = AIContext(organization_id=organization_id, mission_id=mission_id, agent_key=agent_key,
                        run_id=f"run-{uuid.uuid4().hex[:12]}" if agent_key else None, level_override=level_override)
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)
        if recorder is not None and context.records:
            recorder.persist(context)


# ---------------------------------------------------------------------------------------------------------
# The routed provider
# ---------------------------------------------------------------------------------------------------------


class RoutedLLMProvider(LLMProvider):
    """Routes each operation to the LIGHT or ADVANCED provider (chosen before the call)."""

    def __init__(self, *, light: LLMProvider, advanced: LLMProvider, recorder: Any = None) -> None:
        self._light, self._advanced, self.recorder = light, advanced, recorder
        self.name = light.name
        self.is_live = bool(light.is_live)

    @property
    def model_name(self) -> Optional[str]:
        if not self.is_live:
            return None
        light, advanced = self._light.model_name, self._advanced.model_name
        return light if light == advanced else f"{light} | {advanced}"

    @property
    def providers(self) -> Dict[IntelligenceLevel, LLMProvider]:
        return {IntelligenceLevel.LIGHT: self._light, IntelligenceLevel.ADVANCED: self._advanced}

    def __getattr__(self, item: str) -> Any:  # e.g. stats(), test seams on the underlying provider
        light = self.__dict__.get("_light")
        if light is None:
            raise AttributeError(item)
        return getattr(light, item)

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        context = current_ai_context()
        decision = route(operation, context.level_override if context is not None else None)
        provider = self._advanced if decision.level == IntelligenceLevel.ADVANCED else self._light
        if self.recorder is not None and context is not None and context.organization_id is not None:
            self.recorder.check_budget(context.organization_id)  # raises AIBudgetExceededError
        take_usage = getattr(provider, "take_last_usage", None)
        if take_usage:
            take_usage()  # clear anything left over on this thread
        started = time.perf_counter()
        success, error_kind = False, None
        try:
            result = getattr(provider, operation)(*args, **kwargs)
            success = True
            return result
        except LLMProviderError as exc:
            error_kind = type(exc).__name__
            raise  # explicit: never downgraded to another model, never replaced by mock output
        finally:
            if context is not None:
                usage = take_usage() if take_usage else None
                input_tokens, output_tokens = usage if usage else (None, None)
                model = provider.model_name or decision.model
                context.records.append(UsageRecord(
                    operation=operation, level=decision.level, model=model, provider=provider.name,
                    mission_id=context.mission_id, agent_key=context.agent_key, run_id=context.run_id, input_tokens=input_tokens, output_tokens=output_tokens,
                    latency_ms=int((time.perf_counter() - started) * 1000), success=success, error_kind=error_kind,
                    estimated_cost_usd=estimate_cost(model, input_tokens, output_tokens) if provider.is_live else None,
                    created_at=datetime.now(timezone.utc),
                ))

    # --- the LLMProvider operations -------------------------------------------------------------------------

    def classify_academic_intent(self, *a: Any, **k: Any) -> Any:
        return self._call("classify_academic_intent", *a, **k)

    def generate_academic_response(self, *a: Any, **k: Any) -> Any:
        return self._call("generate_academic_response", *a, **k)

    def plan_enquiry(self, *a: Any, **k: Any) -> Any:
        return self._call("plan_enquiry", *a, **k)

    def plan_permission_request(self, *a: Any, **k: Any) -> Any:
        return self._call("plan_permission_request", *a, **k)

    def plan_faculty_query(self, *a: Any, **k: Any) -> Any:
        return self._call("plan_faculty_query", *a, **k)

    def plan_hod_query(self, *a: Any, **k: Any) -> Any:
        return self._call("plan_hod_query", *a, **k)

    def plan_admin_query(self, *a: Any, **k: Any) -> Any:
        return self._call("plan_admin_query", *a, **k)

    def plan_mission(self, *a: Any, **k: Any) -> Any:
        return self._call("plan_mission", *a, **k)

    def classify_career_intent(self, *a: Any, **k: Any) -> Any:
        return self._call("classify_career_intent", *a, **k)

    def generate_career_response(self, *a: Any, **k: Any) -> Any:
        return self._call("generate_career_response", *a, **k)

    def classify_events_intent(self, *a: Any, **k: Any) -> Any:
        return self._call("classify_events_intent", *a, **k)

    def generate_events_response(self, *a: Any, **k: Any) -> Any:
        return self._call("generate_events_response", *a, **k)

    def classify_services_intent(self, *a: Any, **k: Any) -> Any:
        return self._call("classify_services_intent", *a, **k)

    def generate_services_response(self, *a: Any, **k: Any) -> Any:
        return self._call("generate_services_response", *a, **k)


def routed(provider: LLMProvider, *, recorder: Any = None) -> RoutedLLMProvider:
    """Wrap one provider for both levels (mock / anthropic / a test double), or re-use a routed one."""
    if isinstance(provider, RoutedLLMProvider):
        if recorder is not None:
            provider.recorder = recorder
        return provider
    return RoutedLLMProvider(light=provider, advanced=provider, recorder=recorder)


def build_routed_provider(name: Optional[str] = None, *, recorder: Any = None) -> LLMProvider:
    """The application's provider: for Groq, two real models (20B light, 120B advanced)."""
    from app.llm.factory import get_llm_provider

    key = (name or os.environ.get("CAMPUSNEXUS_LLM_PROVIDER") or "mock").strip().lower()
    if key == "groq":
        return RoutedLLMProvider(light=get_llm_provider("groq", model=LIGHT_MODEL),
                                 advanced=get_llm_provider("groq", model=ADVANCED_MODEL), recorder=recorder)
    return routed(get_llm_provider(key), recorder=recorder)
