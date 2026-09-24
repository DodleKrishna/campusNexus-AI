"""Anthropic-backed LLMProvider -- the one real-LLM integration.

``.env.example`` already reserves ``ANTHROPIC_API_KEY`` for "agents/
orchestration", so Anthropic is the smallest practical real integration:
one provider SDK, lazily imported (mirrors ``OnnxMiniLMEmbedding``'s lazy
import of chromadb's embedding utilities in app/rag/embeddings.py) so
nothing outside this module -- including the entire test suite and the
default demo/eval runs -- ever needs the ``anthropic`` package installed.

Structured intent output uses tool-calling with a JSON schema built directly
from the Pydantic result model's ``model_json_schema()``, so the result is
parsed and Pydantic-validated, never regex-parsed free text (CLAUDE.md
Structured Output Requirement). Response generation is a plain completion
constrained by a system prompt to only use the already-verified JSON context
it's given.

Phase 9 hardening (live-mode demo reliability):

- Every request sets ``thinking={"type": "disabled"}`` explicitly. On the
  default model (Claude Sonnet 5) omitting ``thinking`` runs *adaptive*
  thinking, and ``max_tokens`` caps thinking + output together -- so the
  small classification budgets would otherwise truncate before the tool
  call completes. Output budgets are also given real headroom.
- Every failure -- missing SDK/credentials, transport errors, truncated or
  refused responses, a missing/invalid tool call -- raises
  ``LLMProviderError``. Nothing here ever falls back to a mock answer.
- The planner is told what each supported agent actually does, how facts
  flow along dependencies, and how to state unsupported requests instead of
  inventing a task; Action Agent tasks carry typed, allowlisted
  ``constraints`` (the only input the Action Agent acts on).
- Invalid dependency indices are no longer silently dropped: they become
  dangling task ids that ``app.graph.validator`` rejects explicitly.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from app.llm.base import LLMProvider, LLMProviderError
from app.rules.action_preconditions import CATEGORY_DEPARTMENTS, VALID_CASE_PRIORITIES
from app.schemas.academic import AcademicIntentResult, AcademicResponseContext, CourseSummary
from app.schemas.agent_chat import EnquiryPlan
from app.schemas.career import CareerIntentResult, CareerResponseContext
from app.schemas.enums import AgentName
from app.schemas.events import EventsIntentResult, EventsResponseContext
from app.schemas.faculty import FacultyQueryPlan
from app.schemas.mission import MissionPlan, MissionTask
from app.schemas.services import ServicesIntentResult, ServicesResponseContext
from app.schemas.workflow import PermissionIntent

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2

_CLASSIFY_MAX_TOKENS = 1024
_PLAN_MAX_TOKENS = 4096
_RESPONSE_MAX_TOKENS = 1024

# Models whose API surface this provider cannot use: they reject forced
# ``tool_choice`` and/or ``thinking={"type": "disabled"}`` with a 400. Failing
# at construction time gives a clear startup error instead of a 400 on the
# first mission during a demo.
_INCOMPATIBLE_MODEL_PREFIXES = ("claude-fable", "claude-mythos", "claude-opus-5-5")

_INTENT_TOOL_NAME = "classify_academic_intent"
_PLAN_TOOL_NAME = "produce_mission_plan"
_CAREER_INTENT_TOOL_NAME = "classify_career_intent"
_EVENTS_INTENT_TOOL_NAME = "classify_events_intent"
_SERVICES_INTENT_TOOL_NAME = "classify_services_intent"

_ENQUIRY_TOOL_NAME = "plan_enquiry"
_SYSTEM_ENQUIRY_PROMPT = (
    "You route a student's campus question to read-only specialist agents. You never answer the question "
    "yourself and never compute anything. Choose only the specialists whose data the question needs, and write "
    "one short self-contained question for each. For a broad question such as 'what do I have today' or "
    "'anything important', consult academic twice (timetable, and exam schedule), plus events, placements "
    "(deadlines and application status) and complaints (overdue cases). If the student asks to perform an action "
    "(register, apply, file or cancel something), set action_requested and action_agent -- no action is ever taken "
    "here. If the student asks whether a class has actually started, whether attendance is being taken, whether "
    "they were marked present, or which class is happening now or next, set asks_live_class_status (live attendance "
    "session data answers that part; no consultation is needed for it). Put anything outside all four specialists "
    "in out_of_scope."
)

_PERMISSION_TOOL_NAME = "interpret_permission_request"
_SYSTEM_PERMISSION_PROMPT = (
    "You interpret a student's request for permission, leave or on-duty (OD) into a structured form for a campus "
    "workflow. You never grant anything, never choose who reviews it, never invent an event, date or reason, and "
    "never compute a date: report the day only as today, tomorrow, yesterday or an explicit date the student wrote. "
    "event_reference must be words copied from the message. If the student gave no reason, leave reason null."
)

_FACULTY_QUERY_TOOL_NAME = "classify_faculty_query"
_SYSTEM_FACULTY_QUERY_PROMPT = (
    "You classify a faculty member's question about their own classes into a structured intent for a campus "
    "assistant. You never answer, count or compute anything -- the system computes every number from attendance "
    "records. Copy any course or section reference from the question verbatim; never invent one."
)

_SYSTEM_INTENT_PROMPT = (
    "You classify a student's academic support request into a structured intent for a "
    "campus assistant. You do not answer the question yourself and you do not perform any "
    "calculation -- you only classify intent and, if the query names a course, extract the "
    "exact substring of the query that refers to it. Never invent a course reference that "
    "does not literally appear in the query. Distinguish attendance_status (the current figure "
    "only) from attendance_recovery (how many more classes are needed, or how to reach/recover "
    "the required attendance) using the intent definitions in the tool schema."
)

_SYSTEM_CAREER_INTENT_PROMPT = (
    "You classify a student's career/internship support request into a structured intent for "
    "a campus assistant. You do not answer the question yourself, compute eligibility, or "
    "invent an opportunity -- you only classify intent. Identifying the student's missing or "
    "lacking skills (skill gaps) is opportunity_discovery: skill gaps are derived from the open "
    "opportunities the student is evaluated against."
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

# What each agent can actually do -- the planner only ever sees entries for
# agents that are registered (``supported_agents``), so it cannot be steered
# toward a capability that doesn't exist in this deployment.
AGENT_CAPABILITIES: Dict[AgentName, str] = {
    AgentName.ACADEMIC_AGENT: (
        "Answers one academic question per task about the student's own enrolled courses: "
        "attendance percentage in a course; how many more classes are needed to reach the required "
        "attendance (recovery); exam eligibility based on attendance; the weekly class timetable; the "
        "exam schedule; academic policy questions. A timetable task and an exam-schedule task publish "
        "the student's timetable/exams as facts for tasks that depend on them."
    ),
    AgentName.CAREER_AGENT: (
        "Finds internships/jobs the student is eligible for (optionally for a topic such as AI), "
        "identifies the student's skill gaps against them, reports application status, and answers "
        "placement policy questions. Publishes the student's skill gaps as facts for tasks that depend on it."
    ),
    AgentName.EVENTS_OPPORTUNITY_AGENT: (
        "Finds campus events/workshops/competitions matching a topic and/or upstream skill gaps, with "
        "capacity and the student's registration status. It checks schedule clashes ONLY against timetable/"
        "exam facts from Academic Agent tasks it depends on -- so when the goal cares about not clashing with "
        "classes or exams, the events task must depend on an Academic Agent timetable task AND an Academic "
        "Agent exam-schedule task. To match workshops to skill gaps, it must depend on a Career Agent task."
    ),
    AgentName.CAMPUS_SERVICES_AGENT: (
        "Read-only: lists the student's campus complaints/grievance cases with their SLA status (which are "
        "overdue) and answers grievance-procedure questions. It cannot file anything."
    ),
    AgentName.ACTION_AGENT: (
        "Prepares exactly ONE real-world action per task for human approval (nothing executes without an "
        "explicit human decision). Every action_agent task MUST set `action`. Supported tools: "
        "register_event (requires the exact event title the student named. The register_event task MUST depend on "
        "three tasks that do not depend on each other and so run in parallel: an events task that finds that event, "
        "an Academic Agent timetable task (\"What is my timetable?\") and an Academic Agent exam-schedule task "
        "(\"When are my exams?\"). The action task itself checks the event against those timetable/exam facts "
        "before asking for approval, so the events task does not need to depend on the Academic tasks); "
        "create_calendar_event (a personal calendar entry; `title` required, optional "
        "`source_event_title` to anchor it to a named campus event, `lead_time_hours`, `duration_hours`); "
        "create_campus_case (file a complaint; `category`, `description` and optional `priority` required as "
        "stated by the student)."
    ),
}

_SYSTEM_PLAN_PROMPT = (
    "You decompose a student's goal into a structured mission plan for a campus assistant. You only "
    "decide which tasks to run, which agent runs each, and which tasks depend on which. You do not "
    "execute anything and you never compute an official-rule result (attendance, eligibility, "
    "deadlines, SLA, conflicts) yourself -- the agents and a deterministic validator do that.\n\n"
    "Rules:\n"
    "1. Assign every task to one of the supported agents listed below; never use any other agent name.\n"
    "2. One task per distinct question. Each objective must be self-contained and phrased so that agent "
    "can act on it alone -- carry forward any subject (e.g. a course name) that the goal only states once.\n"
    "3. Number tasks 1..N. A task may only depend on lower-numbered tasks, and only when it genuinely needs "
    "that task's output (see each agent's description for which facts flow along a dependency). "
    "Independent tasks must have no dependencies so they can run in parallel.\n"
    "4. Never invent a value the student did not state. In particular, never pick an event to register for, "
    "a complaint category/description, or a calendar title on the student's behalf. If the student wants an "
    "action but has not given what it needs, plan only the read-only discovery tasks and explain what is "
    "missing in `unsupported_requests`. For example, a student who wants to register but has not named an exact "
    "event gets NO action_agent task: plan an Academic timetable task, an Academic exam-schedule task, and an "
    "events task that depends on both (so the candidates are checked for clashes), and state in "
    "`unsupported_requests` that the student must choose one event by its exact title, and list the action's tool "
    "in `selection_required_actions`. An action_agent task whose "
    "target the student did not name word-for-word is refused before it runs.\n"
    "5. If part of the goal is outside every supported agent's capabilities, do not create a task for it; add a "
    "short plain-language explanation to `unsupported_requests`. If nothing in the goal is supported, return "
    "zero tasks.\n"
    "6. Set requires_evidence to true when the answer depends on campus policy text."
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
_SYSTEM_EVENTS_RESPONSE_PROMPT = _response_prompt("campus events/opportunity") + (
    " Only say an event has no schedule conflict when its conflict_check_performed is true; when it is "
    "false, say that schedule conflicts were not checked for that event. When skill_gaps is present, only "
    "say an event addresses a skill gap listed in that event's matched_skill_gaps; if no event matches, say "
    "plainly that no matching event was found for those skill gaps. These are candidate events: never say "
    "the student has chosen, selected or been registered for one unless already_registered is true."
)
_SYSTEM_SERVICES_RESPONSE_PROMPT = _response_prompt("campus services/grievance")


class _ActionSpec(BaseModel):
    """The allowlisted structured input for one Action Agent task.

    Only these keys ever reach ``MissionTask.constraints``; the Action Agent,
    Deterministic Verifier and Tool Gateway still validate every value before
    anything is proposed, approved, or executed.
    """

    tool_name: Literal["register_event", "create_calendar_event", "create_campus_case"]
    event_title: Optional[str] = Field(default=None, description="register_event: the exact event title the student named.")
    title: Optional[str] = Field(default=None, description="create_calendar_event: the calendar entry title.")
    source_event_title: Optional[str] = Field(
        default=None, description="create_calendar_event: exact title of the campus event this entry is for, if any."
    )
    lead_time_hours: Optional[float] = Field(
        default=None, description="create_calendar_event: hours before the source event the entry should start."
    )
    duration_hours: Optional[float] = Field(default=None, description="create_calendar_event: entry length in hours.")
    category: Optional[str] = Field(
        default=None, description=f"create_campus_case: one of {sorted(CATEGORY_DEPARTMENTS)}."
    )
    description: Optional[str] = Field(default=None, description="create_campus_case: the complaint text, as stated by the student.")
    priority: Optional[str] = Field(default=None, description=f"create_campus_case: one of {sorted(VALID_CASE_PRIORITIES)}.")

    def to_constraints(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True)


class _PlannedTask(BaseModel):
    """One task as proposed by the model -- dependencies are by index, not id,
    so the model never has to invent/repeat unique string ids consistently;
    ``_proposal_to_plan`` generates real ``task_id``s and resolves indices."""

    index: int = Field(ge=1)
    objective: str
    agent: AgentName
    depends_on_indices: List[int] = Field(default_factory=list)
    requires_evidence: bool = True
    action: Optional[_ActionSpec] = Field(default=None, description="Required for action_agent tasks; omit otherwise.")


class _PlanProposal(BaseModel):
    tasks: List[_PlannedTask] = Field(default_factory=list)
    unsupported_requests: List[str] = Field(
        default_factory=list,
        description="Plain-language notes on any part of the goal that no supported agent/tool can handle.",
    )
    selection_required_actions: List[Literal["register_event", "create_calendar_event"]] = Field(
        default_factory=list,
        description=(
            "Action tools the student asked for (e.g. register for an event) without naming the exact target "
            "resource, so no action_agent task was planned for them and the student must select one first. "
            "Leave empty when the student named the target, asked only for information or recommendations, "
            "or asked for something no tool supports."
        ),
    )


def _task_id(mission_id: str, index: int) -> str:
    return f"{mission_id}-task-{index}"


def _proposal_to_plan(mission_id: str, goal: str, proposal: _PlanProposal) -> MissionPlan:
    """Convert the model's proposal into a MissionPlan without repairing it.

    A dependency on an index that doesn't exist becomes a dangling task id
    (rejected by ``app.graph.validator``), and a duplicate index becomes a
    duplicate task id (also rejected) -- the plan is never silently "fixed".
    """
    tasks = [
        MissionTask(
            task_id=_task_id(mission_id, t.index),
            mission_id=mission_id,
            agent=t.agent,
            objective=t.objective,
            dependencies=[_task_id(mission_id, i) for i in t.depends_on_indices],
            requires_evidence=t.requires_evidence,
            constraints=t.action.to_constraints() if (t.action is not None and t.agent == AgentName.ACTION_AGENT) else {},
        )
        for t in proposal.tasks
    ]
    notes = [note.strip() for note in proposal.unsupported_requests if note and note.strip()]
    return MissionPlan(
        mission_id=mission_id, goal=goal, tasks=tasks, unsupported_requests=notes,
        selection_required_actions=list(dict.fromkeys(proposal.selection_required_actions)),
    )


def _check_model_compatible(model: str) -> None:
    if model.startswith(_INCOMPATIBLE_MODEL_PREFIXES):
        raise LLMProviderError(
            f"CAMPUSNEXUS_LLM_MODEL={model!r} is not supported by this provider: it rejects the forced tool "
            "calls / disabled thinking used for structured output. Use claude-sonnet-5 (default), "
            "claude-opus-5, or claude-haiku-4-5."
        )


class AnthropicLLMProvider(LLMProvider):
    """Real LLM provider (Anthropic Claude) behind the LLMProvider abstraction."""

    name = "anthropic"
    is_live = True

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        client: Any = None,
    ) -> None:
        """``client`` is a test seam: an object exposing ``messages.create``.
        When omitted, a real ``anthropic.Anthropic`` client is built."""
        self._model = model or os.environ.get("CAMPUSNEXUS_LLM_MODEL") or DEFAULT_MODEL
        _check_model_compatible(self._model)

        if client is not None:
            self._client = client
            return

        try:
            import anthropic  # lazy import: only required when this provider is selected
        except ImportError as exc:
            raise LLMProviderError(
                "CAMPUSNEXUS_LLM_PROVIDER=anthropic requires the 'anthropic' package. "
                'Install it with: pip install -e ".[dev,llm]"'
            ) from exc

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise LLMProviderError(
                "CAMPUSNEXUS_LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY to be set (see .env.example). "
                "Unset CAMPUSNEXUS_LLM_PROVIDER (or set it to 'mock') to run the offline demo instead."
            )
        resolved_timeout = timeout if timeout is not None else float(
            os.environ.get("CAMPUSNEXUS_LLM_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS
        )
        self._client = anthropic.Anthropic(
            api_key=resolved_key,
            timeout=resolved_timeout,
            max_retries=DEFAULT_MAX_RETRIES if max_retries is None else max_retries,
        )

    @property
    def model_name(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    # Request plumbing -- every call goes through _create, so every call gets
    # the same thinking/stop_reason/error handling.
    # ------------------------------------------------------------------

    def _create(self, *, max_tokens: int, system: str, content: str, tool: Optional[Dict[str, Any]] = None):
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "thinking": {"type": "disabled"},
        }
        if tool is not None:
            kwargs["tools"] = [tool]
            kwargs["tool_choice"] = {"type": "tool", "name": tool["name"]}
        try:
            response = self._client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 -- provider boundary: every SDK/transport failure becomes one typed error
            raise LLMProviderError(f"Anthropic API call failed ({type(exc).__name__}): {exc}") from exc

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "max_tokens":
            raise LLMProviderError(f"Anthropic response was truncated at max_tokens={max_tokens}; output discarded.")
        if stop_reason == "refusal":
            raise LLMProviderError("Anthropic declined to answer this request (stop_reason=refusal).")
        return response

    def _structured(self, *, max_tokens: int, system: str, content: str, tool_name: str, description: str, schema_cls: type):
        response = self._create(
            max_tokens=max_tokens,
            system=system,
            content=content,
            tool={"name": tool_name, "description": description, "input_schema": schema_cls.model_json_schema()},
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
                try:
                    return schema_cls.model_validate(block.input)
                except ValidationError as exc:
                    raise LLMProviderError(f"Anthropic '{tool_name}' output failed schema validation: {exc}") from exc
        raise LLMProviderError(f"Anthropic response did not include the expected '{tool_name}' tool call.")

    def _text(self, *, system: str, context: BaseModel) -> str:
        response = self._create(
            max_tokens=_RESPONSE_MAX_TOKENS,
            system=system,
            content=json.dumps(context.model_dump(mode="json")),
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text").strip()
        if not text:
            raise LLMProviderError("Anthropic returned an empty response.")
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
            raise LLMProviderError(f"Anthropic produced a structurally invalid mission plan: {exc}") from exc

    # ------------------------------------------------------------------
    # Specialist agents -- same tool-calling / plain-completion shape as the
    # Academic Agent pair above.
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
