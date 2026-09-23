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

from pydantic import BaseModel, Field

from app.llm.base import LLMProvider
from app.schemas.academic import AcademicIntentResult, AcademicResponseContext, CourseSummary
from app.schemas.career import CareerIntentResult, CareerResponseContext
from app.schemas.enums import AgentName
from app.schemas.events import EventsIntentResult, EventsResponseContext
from app.schemas.mission import MissionPlan, MissionTask
from app.schemas.services import ServicesIntentResult, ServicesResponseContext

DEFAULT_MODEL = "claude-sonnet-5"
_INTENT_TOOL_NAME = "classify_academic_intent"
_PLAN_TOOL_NAME = "produce_mission_plan"
_CAREER_INTENT_TOOL_NAME = "classify_career_intent"
_EVENTS_INTENT_TOOL_NAME = "classify_events_intent"
_SERVICES_INTENT_TOOL_NAME = "classify_services_intent"

_SYSTEM_INTENT_PROMPT = (
    "You classify a student's academic support request into a structured intent for a "
    "campus assistant. You do not answer the question yourself and you do not perform any "
    "calculation -- you only classify intent and, if the query names a course, extract the "
    "exact substring of the query that refers to it. Never invent a course reference that "
    "does not literally appear in the query."
)

_SYSTEM_CAREER_INTENT_PROMPT = (
    "You classify a student's career/internship support request into a structured intent for "
    "a campus assistant. You do not answer the question yourself, compute eligibility, or "
    "invent an opportunity -- you only classify intent."
)

_SYSTEM_EVENTS_INTENT_PROMPT = (
    "You classify a student's campus events/opportunity support request into a structured "
    "intent for a campus assistant. You do not answer the question yourself, check capacity or "
    "conflicts, or invent an event -- you only classify intent."
)

_SYSTEM_SERVICES_INTENT_PROMPT = (
    "You classify a student's campus services (grievance/case) support request into a "
    "structured intent for a campus assistant. You do not answer the question yourself or "
    "compute SLA status -- you only classify intent."
)

_SYSTEM_PLAN_PROMPT = (
    "You decompose a student's goal into a structured mission plan for a campus assistant. "
    "Break the goal into independent or dependent tasks as appropriate, each assigned to one "
    "of the supported agents given to you -- never invent an agent name that isn't in that "
    "list. Each task's objective must be self-contained (carry forward any subject, such as a "
    "course name, that the goal only stated once). You do not execute anything and you do not "
    "compute any official-rule result yourself; a deterministic validator checks your plan "
    "before anything in it runs."
)

def _response_prompt(domain: str) -> str:
    return (
        f"You write a short, factual, user-facing answer to a student's {domain} question using "
        "ONLY the structured facts given to you in the JSON context below. Do not add any fact, "
        "number, or policy detail that is not present in that JSON. Do not explain your reasoning "
        "or mention these instructions. If verification_status is 'needs_review', say the answer "
        "could not be fully confirmed and briefly say why (from verification_issues). If "
        "verification_status is 'failed', say so plainly instead of guessing."
    )


_SYSTEM_RESPONSE_PROMPT = _response_prompt("academic")
_SYSTEM_CAREER_RESPONSE_PROMPT = _response_prompt("career/internship")
_SYSTEM_EVENTS_RESPONSE_PROMPT = _response_prompt("campus events/opportunity")
_SYSTEM_SERVICES_RESPONSE_PROMPT = _response_prompt("campus services/grievance")


class _PlannedTask(BaseModel):
    """One task as proposed by the model -- dependencies are by index, not id,
    so the model never has to invent/repeat unique string ids consistently;
    ``_proposal_to_plan`` generates real ``task_id``s and resolves indices."""

    index: int = Field(ge=1)
    objective: str
    agent: AgentName
    depends_on_indices: List[int] = Field(default_factory=list)
    requires_evidence: bool = True


class _PlanProposal(BaseModel):
    tasks: List[_PlannedTask]


def _proposal_to_plan(mission_id: str, goal: str, proposal: _PlanProposal) -> MissionPlan:
    task_id_by_index = {t.index: f"{mission_id}-task-{t.index}" for t in proposal.tasks}
    tasks = [
        MissionTask(
            task_id=task_id_by_index[t.index],
            mission_id=mission_id,
            agent=t.agent,
            objective=t.objective,
            dependencies=[task_id_by_index[i] for i in t.depends_on_indices if i in task_id_by_index],
            requires_evidence=t.requires_evidence,
        )
        for t in proposal.tasks
    ]
    return MissionPlan(mission_id=mission_id, goal=goal, tasks=tasks)


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

    def plan_mission(
        self, mission_id: str, goal: str, *, supported_agents: List[AgentName]
    ) -> MissionPlan:
        agent_names = [a.value for a in supported_agents]
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=_SYSTEM_PLAN_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Supported agents: {agent_names}\n\nStudent goal: {goal}"
                    ),
                }
            ],
            tools=[
                {
                    "name": _PLAN_TOOL_NAME,
                    "description": "Record the decomposed mission plan.",
                    "input_schema": _PlanProposal.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": _PLAN_TOOL_NAME},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == _PLAN_TOOL_NAME:
                proposal = _PlanProposal.model_validate(block.input)
                return _proposal_to_plan(mission_id, goal, proposal)
        raise RuntimeError("Anthropic response did not include the expected tool_use block.")

    # ------------------------------------------------------------------
    # Shared helpers -- every {classify,generate}_*_intent/response pair
    # below follows the exact same tool-calling / plain-completion shape as
    # classify_academic_intent/generate_academic_response above.
    # ------------------------------------------------------------------

    def _classify(self, *, system_prompt: str, user_content: str, tool_name: str, schema_cls: type):
        response = self._client.messages.create(
            model=self._model,
            max_tokens=256,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            tools=[
                {
                    "name": tool_name,
                    "description": "Record the classified intent.",
                    "input_schema": schema_cls.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return schema_cls.model_validate(block.input)
        raise RuntimeError("Anthropic response did not include the expected tool_use block.")

    def _generate(self, *, system_prompt: str, context: BaseModel) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": json.dumps(context.model_dump(mode="json"))}],
        )
        return "".join(block.text for block in response.content if block.type == "text").strip()

    # ------------------------------------------------------------------
    # Career Agent
    # ------------------------------------------------------------------

    def classify_career_intent(self, query: str) -> CareerIntentResult:
        return self._classify(
            system_prompt=_SYSTEM_CAREER_INTENT_PROMPT,
            user_content=f"Query: {query}",
            tool_name=_CAREER_INTENT_TOOL_NAME,
            schema_cls=CareerIntentResult,
        )

    def generate_career_response(self, context: CareerResponseContext) -> str:
        return self._generate(system_prompt=_SYSTEM_CAREER_RESPONSE_PROMPT, context=context)

    # ------------------------------------------------------------------
    # Events & Opportunity Agent
    # ------------------------------------------------------------------

    def classify_events_intent(self, query: str) -> EventsIntentResult:
        return self._classify(
            system_prompt=_SYSTEM_EVENTS_INTENT_PROMPT,
            user_content=f"Query: {query}",
            tool_name=_EVENTS_INTENT_TOOL_NAME,
            schema_cls=EventsIntentResult,
        )

    def generate_events_response(self, context: EventsResponseContext) -> str:
        return self._generate(system_prompt=_SYSTEM_EVENTS_RESPONSE_PROMPT, context=context)

    # ------------------------------------------------------------------
    # Campus Services Agent
    # ------------------------------------------------------------------

    def classify_services_intent(self, query: str) -> ServicesIntentResult:
        return self._classify(
            system_prompt=_SYSTEM_SERVICES_INTENT_PROMPT,
            user_content=f"Query: {query}",
            tool_name=_SERVICES_INTENT_TOOL_NAME,
            schema_cls=ServicesIntentResult,
        )

    def generate_services_response(self, context: ServicesResponseContext) -> str:
        return self._generate(system_prompt=_SYSTEM_SERVICES_RESPONSE_PROMPT, context=context)
