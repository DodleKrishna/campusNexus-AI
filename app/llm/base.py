"""Provider-neutral LLM abstraction, shared by every specialist agent and the
Mission Orchestrator (Phase 5 added ``plan_mission``; Phase 6 adds one
classify/respond method pair per new specialist agent, to the same
interface, rather than introducing a second, parallel LLM abstraction).

No provider is ever called directly by application code (CLAUDE.md: LLMs
choose *what to do*, never compute official-rule results). All methods are
structured except each agent's final response, which is the one place
CLAUDE.md allows free text:

- ``classify_academic_intent`` / ``classify_career_intent`` /
  ``classify_events_intent`` / ``classify_services_intent``: structured
  intent (+ a proposed, unverified course reference for Academic).
- ``generate_academic_response`` / ``generate_career_response`` /
  ``generate_events_response`` / ``generate_services_response``: free-text
  explanation, but generated only from an already-verified response context
  -- never used for arithmetic, eligibility, conflict, SLA, or policy truth.
- ``plan_mission``: decomposes a free-form goal into a structured
  ``MissionPlan`` (task decomposition/agent assignment/dependencies only --
  never executes anything itself; the Orchestrator's deterministic validator
  is what a plan must clear before any task is dispatched).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from app.schemas.academic import AcademicIntentResult, AcademicResponseContext, CourseSummary
from app.schemas.agent_chat import EnquiryPlan
from app.schemas.career import CareerIntentResult, CareerResponseContext
from app.schemas.enums import AgentName
from app.schemas.events import EventsIntentResult, EventsResponseContext
from app.schemas.faculty import FacultyQueryPlan
from app.schemas.department import HodQueryPlan
from app.schemas.admin_console import AdminQueryPlan
from app.schemas.mission import MissionPlan
from app.schemas.services import ServicesIntentResult, ServicesResponseContext
from app.schemas.workflow import PermissionIntent


class LLMProviderError(RuntimeError):
    """A provider call failed or returned output that cannot be trusted.

    Covers configuration problems (missing credentials/SDK), transport
    failures (auth, rate limit, network, timeout), and unusable responses
    (truncated, refused, missing the expected structured tool call, failing
    Pydantic validation). Callers must surface it as a failure -- never
    substitute a mock/default answer in its place.
    """


class LLMMalformedOutputError(LLMProviderError):
    """The provider answered, but its output is unusable: truncated, not JSON, or rejected by the
    provider's own schema check. Discarded, never repaired (AgentOS V2: the decision is rejected)."""


class LLMTransientError(LLMProviderError):
    """An infrastructure failure that persisted through the provider's own
    bounded retries: rate limit, timeout, network error or 5xx (Phase 12C).

    It says nothing about whether the mission plan is right, so the
    Orchestrator never replans on it: the affected task stays PENDING, the
    mission stops with a provider-unavailable reason, and a resume continues
    from there. ``details()`` carries no secrets.
    """

    kind = "transient"

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: Optional[str],
        status_code: Optional[int] = None,
        retry_after_seconds: Optional[float] = None,
        retries: int = 0,
        kind: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retries = retries
        if kind is not None:
            self.kind = kind

    def details(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "status_code": self.status_code,
            "retry_after_seconds": self.retry_after_seconds,
            "retries": self.retries,
        }


class LLMRateLimitError(LLMTransientError):
    """HTTP 429 from the provider after honoring its retry-after guidance."""

    kind = "rate_limit"


class LLMProvider(ABC):
    """A swappable LLM backend for the Academic Agent and the Mission Orchestrator."""

    name: str
    # True only for a provider that calls a real model over the network --
    # surfaced by GET /health and the UI so a demo never presents
    # deterministic mock output as live AI output.
    is_live: bool = False

    @property
    def model_name(self) -> Optional[str]:
        """The concrete model id in use, or None for an offline provider."""
        return None

    @abstractmethod
    def classify_academic_intent(
        self, query: str, enrolled_courses: List[CourseSummary]
    ) -> AcademicIntentResult:
        """Classify a natural-language academic query into a structured intent.

        ``enrolled_courses`` is provided as context only (so the provider can
        recognize course names/codes in the query) -- the returned
        ``raw_course_reference`` is a proposal only; it is not trusted as a
        verified course until ``app.agents.academic.course_resolution.resolve_course``
        resolves it against the student's real enrollments.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_academic_response(self, context: AcademicResponseContext) -> str:
        """Render a final, user-facing explanation from already-verified structured facts.

        Must not assert certainty beyond what ``context.verification_status``
        supports, must not expose chain-of-thought, and must not introduce
        any fact not already present in ``context``.
        """
        raise NotImplementedError

    def plan_enquiry(self, query: str) -> EnquiryPlan:
        """Phase 15: decide which specialists the read-only Enquiry Agent should
        consult for ``query`` (structured output only -- it never answers).
        Providers that don't support it fail visibly rather than guess."""
        raise LLMProviderError(f"LLM provider {getattr(self, 'name', type(self).__name__)!r} does not support enquiry planning.")

    def plan_permission_request(self, message: str) -> PermissionIntent:
        """Phase 16: interpret a student's permission/leave/OD message into a
        structured ``PermissionIntent``. It never chooses a reviewer, resolves an
        event or computes a date -- deterministic code does all of that."""
        raise LLMProviderError(f"LLM provider {getattr(self, 'name', type(self).__name__)!r} does not support permission requests.")

    def plan_faculty_query(self, message: str) -> FacultyQueryPlan:
        """Phase 16: classify a faculty member's question (structured output only).
        Counts and lists are computed in code over the faculty member's own classes."""
        raise LLMProviderError(f"LLM provider {getattr(self, 'name', type(self).__name__)!r} does not support faculty queries.")

    def plan_hod_query(self, message: str) -> HodQueryPlan:
        """Phase 17: classify a head of department's question (structured output only).
        The department is never part of the plan -- it comes from the caller's scope."""
        raise LLMProviderError(f"LLM provider {getattr(self, 'name', type(self).__name__)!r} does not support HOD queries.")

    def plan_admin_query(self, message: str) -> AdminQueryPlan:
        """Phase 18: classify an administrator's institution-wide question (structured output only)."""
        raise LLMProviderError(f"LLM provider {getattr(self, 'name', type(self).__name__)!r} does not support admin queries.")

    @abstractmethod
    def plan_mission(
        self, mission_id: str, goal: str, *, supported_agents: List[AgentName]
    ) -> MissionPlan:
        """Decompose ``goal`` into a structured ``MissionPlan`` assigned across ``supported_agents``.

        A proposal only -- ``app.graph.validator`` deterministically checks
        it (unique task ids, valid agent assignments, a real DAG, etc.)
        before the Orchestrator ever dispatches a task from it.
        """
        raise NotImplementedError

    @abstractmethod
    def classify_career_intent(self, query: str) -> CareerIntentResult:
        """Classify a natural-language career query into a structured intent."""
        raise NotImplementedError

    @abstractmethod
    def generate_career_response(self, context: CareerResponseContext) -> str:
        """Render a final, user-facing explanation from already-verified career facts."""
        raise NotImplementedError

    @abstractmethod
    def classify_events_intent(self, query: str) -> EventsIntentResult:
        """Classify a natural-language events/opportunity query into a structured intent."""
        raise NotImplementedError

    @abstractmethod
    def generate_events_response(self, context: EventsResponseContext) -> str:
        """Render a final, user-facing explanation from already-verified events facts."""
        raise NotImplementedError

    @abstractmethod
    def classify_services_intent(self, query: str) -> ServicesIntentResult:
        """Classify a natural-language campus-services query into a structured intent."""
        raise NotImplementedError

    @abstractmethod
    def generate_services_response(self, context: ServicesResponseContext) -> str:
        """Render a final, user-facing explanation from already-verified campus-services facts."""
        raise NotImplementedError
