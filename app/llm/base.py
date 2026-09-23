"""Provider-neutral LLM abstraction for the Academic Agent.

The Academic Agent depends on this ABC, never on a provider SDK directly
(CLAUDE.md: LLMs choose *what to do*, never compute official-rule results).
Two responsibilities only -- both structured except the final response,
which is the one place CLAUDE.md allows free text:

- ``classify_academic_intent``: structured intent + a proposed (unverified)
  course reference.
- ``generate_academic_response``: free-text explanation, but generated only
  from an already-verified ``AcademicResponseContext`` -- never used for
  arithmetic, threshold interpretation, or policy truth.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from app.schemas.academic import AcademicIntentResult, AcademicResponseContext, CourseSummary


class LLMProvider(ABC):
    """A swappable LLM backend for the Academic Agent."""

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
