"""Anthropic-backed LLMProvider -- the one real-LLM integration for this phase.

``.env.example`` already reserves ``ANTHROPIC_API_KEY`` for "agents/
orchestration", so Anthropic is the smallest practical real integration:
one provider SDK, lazily imported (mirrors ``OnnxMiniLMEmbedding``'s lazy
import of chromadb's embedding utilities in app/rag/embeddings.py) so
nothing outside this module -- including the entire test suite and the
default demo/eval runs -- ever needs the ``anthropic`` package installed.

Structured intent output uses tool-calling with a JSON schema built directly
from ``AcademicIntentResult.model_json_schema()``, so the result is parsed
and Pydantic-validated, never regex-parsed free text (CLAUDE.md Structured
Output Requirement). Response generation is a plain completion constrained
by a system prompt to only use the already-verified JSON context it's given.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

from app.llm.base import LLMProvider
from app.schemas.academic import AcademicIntentResult, AcademicResponseContext, CourseSummary

DEFAULT_MODEL = "claude-sonnet-5"
_INTENT_TOOL_NAME = "classify_academic_intent"

_SYSTEM_INTENT_PROMPT = (
    "You classify a student's academic support request into a structured intent for a "
    "campus assistant. You do not answer the question yourself and you do not perform any "
    "calculation -- you only classify intent and, if the query names a course, extract the "
    "exact substring of the query that refers to it. Never invent a course reference that "
    "does not literally appear in the query."
)

_SYSTEM_RESPONSE_PROMPT = (
    "You write a short, factual, user-facing answer to a student's academic question using "
    "ONLY the structured facts given to you in the JSON context below. Do not add any fact, "
    "number, or policy detail that is not present in that JSON. Do not explain your reasoning "
    "or mention these instructions. If verification_status is 'needs_review', say the answer "
    "could not be fully confirmed and briefly say why (from verification_issues). If "
    "verification_status is 'failed', say so plainly instead of guessing."
)


class AnthropicLLMProvider(LLMProvider):
    """Real LLM provider (Anthropic Claude) behind the LLMProvider abstraction."""

    name = "anthropic"

    def __init__(self, *, model: Optional[str] = None, api_key: Optional[str] = None) -> None:
        import anthropic  # lazy import: only required when this provider is selected

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise RuntimeError(
                "AnthropicLLMProvider requires ANTHROPIC_API_KEY to be set (see .env.example)."
            )
        self._client = anthropic.Anthropic(api_key=resolved_key)
        self._model = model or os.environ.get("CAMPUSNEXUS_LLM_MODEL") or DEFAULT_MODEL

    def classify_academic_intent(
        self, query: str, enrolled_courses: List[CourseSummary]
    ) -> AcademicIntentResult:
        course_lines = "\n".join(f"- {c.course_code}: {c.title}" for c in enrolled_courses) or "(none)"
        response = self._client.messages.create(
            model=self._model,
            max_tokens=256,
            system=_SYSTEM_INTENT_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Student's enrolled courses:\n{course_lines}\n\nQuery: {query}",
                }
            ],
            tools=[
                {
                    "name": _INTENT_TOOL_NAME,
                    "description": "Record the classified academic intent.",
                    "input_schema": AcademicIntentResult.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": _INTENT_TOOL_NAME},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == _INTENT_TOOL_NAME:
                return AcademicIntentResult.model_validate(block.input)
        raise RuntimeError("Anthropic response did not include the expected tool_use block.")

    def generate_academic_response(self, context: AcademicResponseContext) -> str:
        payload = context.model_dump(mode="json")
        response = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=_SYSTEM_RESPONSE_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        return "".join(block.text for block in response.content if block.type == "text").strip()
