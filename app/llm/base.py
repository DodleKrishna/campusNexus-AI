"""Provider-neutral LLM abstraction, shared by the Academic Agent and the
Mission Orchestrator (Phase 5 adds ``plan_mission`` to the same interface
rather than introducing a second, parallel LLM abstraction).

No provider is ever called directly by application code (CLAUDE.md: LLMs
choose *what to do*, never compute official-rule results). All methods are
structured except the final academic response, which is the one place
CLAUDE.md allows free text:

- ``classify_academic_intent``: structured intent + a proposed (unverified)
  course reference.
- ``generate_academic_response``: free-text explanation, but generated only
  from an already-verified ``AcademicResponseContext`` -- never used for
  arithmetic, threshold interpretation, or policy truth.
- ``plan_mission``: decomposes a free-form goal into a structured
  ``MissionPlan`` (task decomposition/agent assignment/dependencies only --
  never executes anything itself; the Orchestrator's deterministic validator
  is what a plan must clear before any task is dispatched).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from app.schemas.academic import AcademicIntentResult, AcademicResponseContext, CourseSummary
from app.schemas.enums import AgentName
from app.schemas.mission import MissionPlan


class LLMProvider(ABC):
    """A swappable LLM backend for the Academic Agent and the Mission Orchestrator."""

    name: str

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
