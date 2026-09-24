"""Live LLM validation for CampusNexus (Phase 9; Phase 12 adds Groq + --smoke).

Exercises a *real* provider (``--provider``, else CAMPUSNEXUS_LLM_PROVIDER when
it names a real provider, else ``anthropic``) -- never the mock -- and checks
every structured output with the existing Pydantic schemas and the
deterministic plan validator:

1. Structured academic intents (with course-reference extraction).
2. Structured Career / Events / Campus Services intents.
3. A ``MissionPlan`` for each natural-language demo goal (several phrasings
   each), validated by ``app.graph.validator.validate_plan`` against the full
   five-agent registry: only supported agents, known dependencies, no cycles;
   plus every Action Agent task carrying an allowlisted ``tool_name``.
4. With ``--e2e``: each goal run end-to-end through the real Orchestrator and
   real agents against a throwaway seeded database (never the dev or demo DB).

``--smoke`` replaces checks 1-3 with five low-cost requests (two intents,
three plans), printing PASS/FAIL, provider, model, validation status, the
agents each plan selected and latency. Smoke mode only classifies and plans:
it never runs a mission, so no action is proposed or executed.

Phase 12B:

- Plans are also checked for target provenance: an action task whose target
  the student did not name is a structured failure (the Orchestrator would
  refuse it anyway).
- E2E results are judged against the *correct* outcome, not "completed":
  an unsupported goal must be a SAFE REFUSAL, an unnamed registration a SAFE
  CLARIFICATION / USER SELECTION REQUIRED (no proposal, no approval), a named
  one AWAITING APPROVAL. Structured correctness and mission outcome are
  reported separately.
- Every E2E run records a full trace (plan, dependencies, the exact
  AgentMessage each agent received, AgentResults, evidence, verification,
  audit trail, approvals) and per-call latency by component (Planner,
  Academic, Career, Events, Services, Action).
- ``--affected`` re-runs only the three scenarios the first live Groq E2E run
  got wrong, with full traces.

With no API key (or no SDK) it prints UNAVAILABLE and exits 2 -- it never
fabricates results or falls back to the mock provider. Intent checks compare
against an expected label and count a mismatch as a failure. API keys are
never printed.

Usage:
    python scripts/check_live_llm.py                 # ~25 small requests
    python scripts/check_live_llm.py --e2e           # + full missions (more requests)
    python scripts/check_live_llm.py --out report.json
    python scripts/check_live_llm.py --provider groq --smoke   # 5 requests
    python scripts/check_live_llm.py --smoke --out data/demo/live_smoke_report.json
    python scripts/check_live_llm.py --provider groq --affected --out data/demo/live_affected_report.json
    python scripts/check_live_llm.py --provider groq --e2e --pause 20   # space missions out under a TPM limit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.graph.orchestrator import unconfirmed_action_targets
from app.graph.registry import AgentRegistry
from app.graph.validator import validate_plan
from app.llm.base import LLMProviderError
from app.llm.factory import LIVE_PROVIDERS, get_llm_provider
from app.schemas.academic import CourseSummary
from app.schemas.enums import AgentName, ApprovalStatus, MissionStatus, UserRole

ALL_AGENTS = [
    AgentName.ACADEMIC_AGENT,
    AgentName.CAREER_AGENT,
    AgentName.EVENTS_OPPORTUNITY_AGENT,
    AgentName.CAMPUS_SERVICES_AGENT,
    AgentName.ACTION_AGENT,
]
ACTION_TOOLS = {"register_event", "create_calendar_event", "create_campus_case"}

# The seeded demo student's enrolments (scripts/seed_data.py), as context only.
COURSES = [
    CourseSummary(course_code="CS301", title="Operating Systems", credits=4, semester=5, instructor="Dr. A"),
    CourseSummary(course_code="CS302", title="Database Management Systems", credits=4, semester=5, instructor="Dr. B"),
    CourseSummary(course_code="CS303", title="Computer Networks", credits=4, semester=5, instructor="Dr. C"),
]

ACADEMIC_INTENTS = [
    ("My OS attendance is low. Can I write the exam?", "exam_eligibility", "OS"),
    ("How many more classes do I need to attend in Computer Networks to reach 75%?", "attendance_recovery", "Computer Networks"),
    ("How can I reach 75% attendance in OS?", "attendance_recovery", "OS"),
    ("How much attendance do I currently have in Computer Networks?", "attendance_status", "Computer Networks"),
    ("What's my timetable this week?", "timetable", None),
    ("When is my DBMS exam?", "exam_schedule", "DBMS"),
]
SPECIALIST_INTENTS = [
    ("career", "Find AI internships I'm eligible for and tell me which skills I'm missing.", "opportunity_discovery"),
    # The objective a live planner wrote for the failed Events E2E (Phase 12B).
    ("career", "Identify my missing technical skills based on my current profile and coursework.", "opportunity_discovery"),
    ("career", "What's the status of my application to the data analyst internship?", "application_status"),
    ("events", "Find a useful workshop for my missing technical skills.", "event_discovery"),
    ("events", "Am I registered for the hackathon kickoff?", "registration_status"),
    ("services", "Check my complaints and tell me whether any are overdue.", "case_status"),
    ("services", "What is the grievance escalation procedure?", "policy_question"),
]
GOALS = {
    "academic": [
        "My OS attendance is low. Can I write the exam, and how can I recover?",
        "Am I allowed to sit the Operating Systems exam, and what do I need to do to fix my attendance?",
    ],
    "career": [
        "Help me prepare for AI internships without missing important classes.",
        "I want an AI internship. What am I missing, and which workshops that fit my timetable would help?",
    ],
    "events": [
        "Find a useful workshop for my missing technical skills.",
        "Are there any workshops this month that don't clash with my classes or exams?",
    ],
    "services": [
        "Check my complaints and tell me whether any are overdue.",
        "Has any of my grievances breached its SLA?",
    ],
    "action": [
        "Find a suitable event, check my schedule and prepare my registration.",
        "Register me for the 'Competitive Coding Contest' if it doesn't clash with my classes or exams.",
    ],
    "unsupported": ["Book me a flight to Goa for the holidays."],
}
UNNAMED_REGISTRATION_GOAL = GOALS["action"][0]
NAMED_REGISTRATION_GOAL = GOALS["action"][1]
ATTENDANCE_RECOVERY_GOAL = "How many more classes do I need to attend in Computer Networks to reach the required attendance?"

# What a correct plan for these goals must contain beyond structural validity.
PLAN_EXPECTATIONS = {
    UNNAMED_REGISTRATION_GOAL: "user_selection",
    NAMED_REGISTRATION_GOAL: "named_registration",
}

# E2E scenarios: (name, goal, expected outcome, extra check). The expected
# outcome is the *correct* behaviour -- a refusal or a clarification is a
# success when it is the safe answer. Missions are never approved here, so no
# write ever executes.
E2E_SCENARIOS = [
    ("academic", GOALS["academic"][0], "completed", None),
    ("career", GOALS["career"][0], "completed", None),
    ("events", GOALS["events"][0], "completed", "events_match_skill_gaps"),
    ("services", GOALS["services"][0], "completed", None),
    ("action_unnamed", UNNAMED_REGISTRATION_GOAL, "clarification", None),
    ("action_named", NAMED_REGISTRATION_GOAL, "awaiting_approval", None),
    ("unsupported", GOALS["unsupported"][0], "refusal", None),
]
# --affected: the three scenarios the first real Groq E2E run got wrong.
AFFECTED_INTENTS = [(ATTENDANCE_RECOVERY_GOAL, "attendance_recovery", "Computer Networks")]
AFFECTED_SCENARIOS = [
    ("A_attendance_recovery", ATTENDANCE_RECOVERY_GOAL, "completed", "academic_intent:attendance_recovery"),
    ("B_events_skill_gaps", GOALS["events"][0], "completed", "events_match_skill_gaps"),
    ("C_unnamed_registration", UNNAMED_REGISTRATION_GOAL, "clarification", None),
]
OUTCOME_LABELS = {
    "completed": "COMPLETED",
    "clarification": "SAFE CLARIFICATION / USER SELECTION REQUIRED",
    "awaiting_approval": "AWAITING APPROVAL (explicitly named target)",
    "refusal": "SAFE REFUSAL (unsupported request)",
}

# Which component each provider method belongs to, for latency reporting.
LLM_COMPONENTS = {
    "plan_mission": "Planner",
    "classify_academic_intent": "Academic",
    "generate_academic_response": "Academic",
    "classify_career_intent": "Career",
    "generate_career_response": "Career",
    "classify_events_intent": "Events",
    "generate_events_response": "Events",
    "classify_services_intent": "Services",
    "generate_services_response": "Services",
}
AGENT_COMPONENTS = {
    AgentName.ACADEMIC_AGENT: "Academic",
    AgentName.CAREER_AGENT: "Career",
    AgentName.EVENTS_OPPORTUNITY_AGENT: "Events",
    AgentName.CAMPUS_SERVICES_AGENT: "Services",
    AgentName.ACTION_AGENT: "Action",
}


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


class TimedProvider:
    """Transparent proxy that records every LLM call's component, latency and
    structured output. Everything else (name, model_name, is_live) passes
    through unchanged, so the Orchestrator and agents cannot tell it apart."""

    def __init__(self, inner: Any, calls: List[Dict[str, Any]]) -> None:
        self._inner = inner
        self.calls = calls

    def __getattr__(self, attr: str) -> Any:
        target = getattr(self._inner, attr)
        component = LLM_COMPONENTS.get(attr)
        if component is None:
            return target

        def timed(*args: Any, **kwargs: Any) -> Any:
            entry: Dict[str, Any] = {"component": component, "method": attr}
            started = time.perf_counter()
            try:
                result = target(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- recorded, then re-raised unchanged
                entry["error"] = str(exc)
                raise
            finally:
                entry["latency_ms"] = _elapsed_ms(started)
                self.calls.append(entry)
            entry["output"] = result.model_dump(mode="json") if hasattr(result, "model_dump") else f"{str(result)[:200]}"
            return result

        return timed


class TracedAgent:
    """Wraps a real agent to record the exact AgentMessage it received, what it
    returned, and how long it took. Behaviour is unchanged."""

    def __init__(self, inner: Any, dispatches: List[Dict[str, Any]]) -> None:
        self._inner = inner
        self._dispatches = dispatches

    def handle(self, message: Any) -> Any:
        entry: Dict[str, Any] = {
            "task_id": message.task_id,
            "agent": message.target.value,
            "component": AGENT_COMPONENTS.get(message.target, message.target.value),
            "message": {"objective": message.objective, "facts": message.facts, "constraints": message.constraints},
        }
        started = time.perf_counter()
        try:
            outcome = self._inner.handle(message)
        except Exception as exc:  # noqa: BLE001 -- recorded, then re-raised for the dispatcher
            entry.update(latency_ms=_elapsed_ms(started), error=f"{type(exc).__name__}: {exc}")
            self._dispatches.append(entry)
            raise
        result, verification = outcome.agent_result, outcome.verification
        entry.update(
            latency_ms=_elapsed_ms(started),
            result={"status": result.status.value, "facts": result.facts, "errors": result.errors},
            evidence=[
                {"evidence_id": e.evidence_id, "document_id": e.document_id, "section": e.section, "snippet": e.snippet[:200]}
                for e in result.evidence
            ],
            verification={
                "status": verification.status.value,
                "checks": [c.model_dump(mode="json") for c in verification.checks],
                "issues": verification.issues,
            },
            response=outcome.response_text,
        )
        self._dispatches.append(entry)
        return outcome

# --smoke: (name, kind, input, expectation). Intents carry the expected label
# (and course reference for academic); plans carry what a passing plan needs.
SMOKE_CHECKS = [
    ("academic_intent", "academic_intent", "My OS attendance is low. Can I write the exam?", ("exam_eligibility", "OS")),
    ("career_intent", "career_intent", "Find AI internships that fit my profile.", ("opportunity_discovery", None)),
    ("multi_agent_plan", "plan", "Help me prepare for AI internships without missing important classes.", "multi_agent"),
    ("action_plan", "plan", "Register me for the Competitive Coding Contest if it doesn't clash with my schedule.", "action"),
    ("unsupported_request", "plan", "Book me a flight to Goa.", "unsupported"),
]


def _registry_for_validation() -> AgentRegistry:
    registry = AgentRegistry()
    for agent in ALL_AGENTS:
        registry.register(agent, lambda s: None)  # validate_plan only calls is_supported()
    return registry


def _plan_view(plan) -> Dict[str, Any]:
    index = {t.task_id: i for i, t in enumerate(plan.tasks, start=1)}
    return {
        "tasks": [
            {
                "n": index[t.task_id],
                "agent": t.agent.value,
                "objective": t.objective,
                "depends_on": [index.get(d, d) for d in t.dependencies],
                "constraints": t.constraints,
            }
            for t in plan.tasks
        ],
        "unsupported_requests": plan.unsupported_requests,
    }


def check_intents(provider, report: Dict[str, Any], academic_intents=None, specialist_intents=None) -> None:
    for query, expected, expected_course in ACADEMIC_INTENTS if academic_intents is None else academic_intents:
        entry: Dict[str, Any] = {"kind": "academic_intent", "component": "Academic", "query": query, "expected": expected}
        started = time.perf_counter()
        try:
            result = provider.classify_academic_intent(query, COURSES)
            entry.update(actual=result.intent.value, course_reference=result.raw_course_reference)
            entry["pass"] = result.intent.value == expected and (
                expected_course is None or (result.raw_course_reference or "").lower() == expected_course.lower()
            )
        except LLMProviderError as exc:
            entry.update(error=str(exc), **{"pass": False})
        entry["latency_ms"] = _elapsed_ms(started)
        report["checks"].append(entry)

    for domain, query, expected in SPECIALIST_INTENTS if specialist_intents is None else specialist_intents:
        entry = {"kind": f"{domain}_intent", "component": domain.capitalize(), "query": query, "expected": expected}
        classify = getattr(provider, f"classify_{domain}_intent")
        started = time.perf_counter()
        try:
            actual = classify(query).intent.value
            entry.update(actual=actual, **{"pass": actual == expected})
        except LLMProviderError as exc:
            entry.update(error=str(exc), **{"pass": False})
        entry["latency_ms"] = _elapsed_ms(started)
        report["checks"].append(entry)


def plan_expectation_errors(goal: str, plan) -> List[str]:
    """Phase 12B: target provenance, and what the goal's plan must contain.

    An action task whose target the student did not name is a planner error
    even though the Orchestrator would refuse to run it.
    """
    errors = [
        f"{task_id}: action target not named by the student ({provenance.reason})"
        for task_id, provenance in unconfirmed_action_targets(plan, goal).items()
    ]
    expectation = PLAN_EXPECTATIONS.get(goal)
    if expectation == "user_selection" and not plan.unsupported_requests:
        errors.append("expected an unsupported_requests note saying the student must choose an event")
    if expectation == "named_registration":
        if not any(t.agent == AgentName.ACTION_AGENT and t.constraints.get("tool_name") == "register_event" for t in plan.tasks):
            errors.append("expected an action_agent register_event task for the named event")
        errors.extend(_register_event_dependency_errors(plan))
    return errors


def check_plans(provider, report: Dict[str, Any]) -> None:
    registry = _registry_for_validation()
    for category, goals in GOALS.items():
        for n, goal in enumerate(goals, start=1):
            mission_id = f"live-{category}-{n}"
            entry: Dict[str, Any] = {"kind": "plan", "component": "Planner", "category": category, "goal": goal}
            started = time.perf_counter()
            try:
                plan = provider.plan_mission(mission_id, goal, supported_agents=ALL_AGENTS)
            except LLMProviderError as exc:
                entry.update(error=str(exc), latency_ms=_elapsed_ms(started), **{"pass": False})
                report["checks"].append(entry)
                continue
            entry["latency_ms"] = _elapsed_ms(started)
            validation = validate_plan(plan, registry, expected_mission_id=mission_id)
            action_errors = [
                f"{t.task_id}: missing/unknown tool_name {t.constraints.get('tool_name')!r}"
                for t in plan.tasks
                if t.agent == AgentName.ACTION_AGENT and t.constraints.get("tool_name") not in ACTION_TOOLS
            ]
            action_errors += plan_expectation_errors(goal, plan)
            entry["plan"] = _plan_view(plan)
            entry["validation_errors"] = validation.errors + action_errors
            if category == "unsupported":
                # A decline is the correct outcome: zero tasks plus an explanation.
                entry["pass"] = not plan.tasks and bool(plan.unsupported_requests)
            else:
                entry["pass"] = bool(plan.tasks) and validation.is_valid and not action_errors
            report["checks"].append(entry)


def _register_event_dependency_errors(plan) -> List[str]:
    """Phase 10 rule: register_event depends on an events task plus two Academic tasks."""
    agents = {t.task_id: t.agent for t in plan.tasks}
    errors = []
    for task in plan.tasks:
        if task.agent != AgentName.ACTION_AGENT or task.constraints.get("tool_name") != "register_event":
            continue
        upstream = [agents.get(d) for d in task.dependencies]
        if AgentName.EVENTS_OPPORTUNITY_AGENT not in upstream or upstream.count(AgentName.ACADEMIC_AGENT) < 2:
            errors.append(f"{task.task_id}: register_event must depend on an events task and two academic tasks")
    return errors


def _smoke_plan_errors(plan, expectation: str, validation_errors: List[str]) -> List[str]:
    agents = {t.agent for t in plan.tasks}
    if expectation == "unsupported":
        return [] if not plan.tasks and plan.unsupported_requests else ["expected zero tasks plus an unsupported_requests note"]
    errors = list(validation_errors)
    if not plan.tasks:
        errors.append("plan has no tasks")
    if expectation == "multi_agent" and (AgentName.CAREER_AGENT not in agents or len(agents) < 2):
        errors.append("expected a career_agent task plus at least one other agent")
    if expectation == "action":
        registrations = [
            t for t in plan.tasks
            if t.agent == AgentName.ACTION_AGENT and t.constraints.get("tool_name") == "register_event"
        ]
        if not registrations:
            errors.append("expected an action_agent register_event task")
        elif not any("competitive coding contest" in str(t.constraints.get("event_title", "")).lower() for t in registrations):
            errors.append("register_event task does not carry the named event_title")
        errors.extend(_register_event_dependency_errors(plan))
    return errors


def run_smoke(provider, report: Dict[str, Any]) -> None:
    """Five small structured calls; classification and planning only -- nothing executes."""
    registry = _registry_for_validation()
    for name, kind, text, expectation in SMOKE_CHECKS:
        entry: Dict[str, Any] = {
            "name": name, "kind": kind, "input": text,
            "provider": provider.name, "model": provider.model_name,
        }
        started = time.perf_counter()
        try:
            if kind == "academic_intent":
                result = provider.classify_academic_intent(text, COURSES)
            elif kind == "career_intent":
                result = provider.classify_career_intent(text)
            else:
                result = provider.plan_mission(f"smoke-{name}", text, supported_agents=ALL_AGENTS)
        except LLMProviderError as exc:
            entry.update(latency_ms=round((time.perf_counter() - started) * 1000), status="provider_error",
                         error=str(exc), **{"pass": False})
            report["smoke"].append(entry)
            continue
        entry["latency_ms"] = round((time.perf_counter() - started) * 1000)

        if kind == "plan":
            validation = validate_plan(result, registry, expected_mission_id=f"smoke-{name}")
            errors = _smoke_plan_errors(result, expectation, validation.errors)
            entry.update(plan=_plan_view(result), agents=sorted({t.agent.value for t in result.tasks}))
        else:
            expected_intent, expected_course = expectation
            entry.update(expected=expected_intent, actual=result.intent.value)
            errors = [] if result.intent.value == expected_intent else [f"intent {result.intent.value!r} != {expected_intent!r}"]
            if expected_course is not None:
                entry["course_reference"] = result.raw_course_reference
                if (result.raw_course_reference or "").lower() != expected_course.lower():
                    errors.append(f"course reference {result.raw_course_reference!r} != {expected_course!r}")
        # The provider already Pydantic-validated the output; what remains is the expectation/plan checks.
        entry.update(status="schema_valid" if not errors else "schema_valid, expectation_failed",
                     errors=errors, **{"pass": not errors})
        report["smoke"].append(entry)


def print_smoke(report: Dict[str, Any]) -> None:
    for check in report["smoke"]:
        print(f"[{'PASS' if check['pass'] else 'FAIL'}] {check['name']:<20} {check['provider']}/{check['model']}  "
              f"{check['latency_ms']:>6} ms  {check['status']}")
        print(f"         input:  {check['input']}")
        if check.get("actual"):
            course = f"  course={check['course_reference']!r}" if "course_reference" in check else ""
            print(f"         intent: {check['actual']} (expected {check['expected']}){course}")
        if "agents" in check:
            print(f"         agents: {', '.join(check['agents']) or '(none)'}")
            for task in check["plan"]["tasks"]:
                print(f"           {task['n']}. {task['agent']:<26} deps={task['depends_on']} {task['objective'][:60]}")
            for note in check["plan"]["unsupported_requests"]:
                print(f"           unsupported: {note[:100]}")
        for problem in [check["error"]] if "error" in check else check.get("errors", []):
            print(f"         error:  {problem}")


def _resolve_provider_name(requested: Optional[str]) -> str:
    if requested:
        return requested.strip().lower()
    configured = (os.environ.get("CAMPUSNEXUS_LLM_PROVIDER") or "").strip().lower()
    return configured if configured in LIVE_PROVIDERS else "anthropic"


def run_e2e(provider, report: Dict[str, Any], scenarios=None, pause_seconds: float = 0.0) -> None:
    """Run each scenario as a real mission on a throwaway seeded DB, with traces."""
    from app.agents.academic.agent import AcademicAgent
    from app.agents.action.agent import ActionAgent
    from app.agents.career.agent import CareerAgent
    from app.agents.events.agent import EventsAgent
    from app.agents.services.agent import ServicesAgent
    from app.db.session import create_db_engine, create_session_factory, init_db
    from app.graph.orchestrator import MissionOrchestrator
    from app.rag.config import get_rag_config
    from app.rag.embeddings import DeterministicHashEmbedding
    from app.rag.ingest import ingest_policy_directory
    from app.rag.retriever import PolicyRetriever
    from app.rag.vector_store import PolicyVectorStore
    from app.services.knowledge import KnowledgeService
    from app.tools.build import build_default_tool_registry
    from scripts.seed_data import run_seed

    tmp = Path(tempfile.mkdtemp(prefix="campusnexus-live-e2e-"))
    engine = create_db_engine(db_path=tmp / "live.db")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        run_seed(session)
    embedding = DeterministicHashEmbedding()
    store = PolicyVectorStore(path=tmp / "chroma", collection_name="live_e2e", embedding_provider=embedding)
    ingest_policy_directory(get_rag_config().policy_dir, vector_store=store)
    knowledge = KnowledgeService(retriever=PolicyRetriever(vector_store=store, embedding_provider=embedding))
    gateway = build_default_tool_registry()

    llm_calls: List[Dict[str, Any]] = []
    dispatches: List[Dict[str, Any]] = []
    timed = TimedProvider(provider, llm_calls)

    def traced(build: Callable[[Any], Any]) -> Callable[[Any], Any]:
        return lambda session: TracedAgent(build(session), dispatches)

    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, traced(lambda s: AcademicAgent(session=s, knowledge_service=knowledge, llm_provider=timed)))
    registry.register(AgentName.CAREER_AGENT, traced(lambda s: CareerAgent(session=s, knowledge_service=knowledge, llm_provider=timed)))
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, traced(lambda s: EventsAgent(session=s, knowledge_service=knowledge, llm_provider=timed)))
    registry.register(AgentName.CAMPUS_SERVICES_AGENT, traced(lambda s: ServicesAgent(session=s, knowledge_service=knowledge, llm_provider=timed)))
    registry.register(AgentName.ACTION_AGENT, traced(lambda s: ActionAgent(session=s, knowledge_service=knowledge, tool_gateway=gateway)))
    orchestrator = MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=timed)

    for index, (name, goal, expected, extra_check) in enumerate(E2E_SCENARIOS if scenarios is None else scenarios):
        if index and pause_seconds:
            time.sleep(pause_seconds)  # stay under a per-minute token limit; not part of any latency figure
        first_call, first_dispatch = len(llm_calls), len(dispatches)
        started = time.perf_counter()
        state = orchestrator.run_mission(goal, user_id="STU-DEMO-001", user_role=UserRole.STUDENT, student_id="STU-DEMO-001")
        seconds = round(time.perf_counter() - started, 1)
        record = _mission_record(session_factory, state, llm_calls[first_call:], dispatches[first_dispatch:])
        record.update(scenario=name, goal=goal, seconds=seconds, expected=expected, extra_check=extra_check)
        record.update(evaluate_e2e(record, expected, extra_check))
        report["e2e"].append(record)


def _mission_record(session_factory, state, llm_calls: List[Dict[str, Any]], dispatches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Everything needed to trace one mission, read back from the Context Service."""
    from app.db.models.mission import ApprovalRecord, ToolCallRecord
    from app.services.context import ContextService
    from sqlalchemy import select

    mission_id = state["mission_id"]
    plan = state.get("plan")
    with session_factory() as session:
        audit = [
            {"event_type": e.event_type, "step_id": e.step_id, "message": e.message, "metadata": e.event_metadata}
            for e in ContextService(session).list_audit_events(mission_id)
        ]
        approvals = [
            {"approval_id": a.approval_id, "step_id": a.step_id, "status": a.status.value}
            for a in session.execute(select(ApprovalRecord).where(ApprovalRecord.mission_id == mission_id)).scalars()
        ]
        tool_calls = [
            {"tool_call_id": t.tool_call_id, "step_id": t.step_id, "tool_name": t.tool_name, "status": t.status.value}
            for t in session.execute(select(ToolCallRecord).where(ToolCallRecord.mission_id == mission_id)).scalars()
        ]
    return {
        "mission_id": mission_id,
        "mission_status": state["mission_status"].value,
        "plan": _plan_view(plan) if plan is not None else None,
        "task_status": {k: v.value for k, v in state.get("task_status", {}).items()},
        "dispatches": dispatches,
        "llm_calls": llm_calls,
        "audit": audit,
        "approvals": approvals,
        "tool_calls": tool_calls,
        "errors": state.get("errors", []),
        "final_result": state.get("final_result"),
    }


def _extra_check_problems(record: Dict[str, Any], extra_check: Optional[str]) -> List[str]:
    if not extra_check:
        return []
    verified = [d for d in record["dispatches"] if (d.get("verification") or {}).get("status") == "verified"]
    if extra_check.startswith("academic_intent:"):
        wanted = extra_check.split(":", 1)[1]
        intents = [d["result"]["facts"].get("intent") for d in verified if d["agent"] == AgentName.ACADEMIC_AGENT.value]
        return [] if wanted in intents else [f"no verified academic task classified {wanted!r} (got {intents})"]
    if extra_check == "events_match_skill_gaps":
        runs = [d for d in verified if d["agent"] == AgentName.EVENTS_OPPORTUNITY_AGENT.value]
        if not runs:
            return ["no verified events task"]
        facts = runs[-1]["result"]["facts"]
        if "matched_against_skill_gaps" not in facts:
            return ["events task did not receive Career skill gaps"]
        unmatched = [a["event"]["title"] for a in facts.get("assessments", []) if not a.get("matched_skill_gaps")]
        return [f"events recommended without a matching skill gap: {unmatched}"] if unmatched else []
    return [f"unknown extra check {extra_check!r}"]


def evaluate_e2e(record: Dict[str, Any], expected: str, extra_check: Optional[str] = None) -> Dict[str, Any]:
    """Judge one mission against its *correct* outcome (pure, over the record).

    Safety holds for every scenario: this harness never approves anything, so
    no tool call may ever have executed, and only an explicitly named action
    may create an approval.
    """
    problems: List[str] = []
    plan = record.get("plan")
    status = record["mission_status"]
    approvals, tool_calls = record["approvals"], record["tool_calls"]
    selection_events = [e for e in record["audit"] if e["event_type"] == "action_target_unconfirmed"]
    executed = [t for t in tool_calls if t["status"] != "pending"]
    if executed:
        problems.append(f"tool call(s) executed without an approval decision: {executed}")

    if plan is None:
        problems.append("planner produced no plan")
    elif expected == "refusal":
        if plan["tasks"] or not plan["unsupported_requests"]:
            problems.append("expected zero tasks plus an unsupported_requests explanation")
        if approvals:
            problems.append("an unsupported goal created an approval")
    elif expected == "clarification":
        if status != MissionStatus.COMPLETED.value:
            problems.append(f"expected a completed discovery mission, got {status}")
        if approvals or tool_calls:
            problems.append("an approval/tool call was created although no target was named")
        if not selection_events and not plan["unsupported_requests"]:
            problems.append("no user-selection requirement was recorded or explained")
    elif expected == "awaiting_approval":
        pending = [a for a in approvals if a["status"] == ApprovalStatus.PENDING.value]
        if status != MissionStatus.NEEDS_APPROVAL.value or not pending:
            problems.append(f"expected a pending approval, got mission {status} with approvals {approvals}")
        sources = [
            (d["result"]["facts"].get("target_provenance") or {}).get("source")
            for d in record["dispatches"]
            if d["agent"] == AgentName.ACTION_AGENT.value and "result" in d
        ]
        if pending and "user_goal" not in sources:
            problems.append(f"approved target does not come from the student's goal (provenance: {sources})")
    elif expected == "completed":
        if status != MissionStatus.COMPLETED.value:
            problems.append(f"expected completed, got {status}")
        if approvals:
            problems.append("a read-only mission created an approval")
    problems += _extra_check_problems(record, extra_check)

    return {
        "outcome_label": OUTCOME_LABELS[expected] if not problems else f"UNEXPECTED ({status})",
        "user_selection_required": bool(selection_events),
        "problems": problems,
        "pass": not problems,
    }


def latency_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """Per-component latency: every LLM call (structured checks and E2E) plus
    every agent task run, which is the only figure the LLM-free Action Agent has."""
    kind_components = {"plan": "Planner", "academic_intent": "Academic", "career_intent": "Career"}
    calls: Dict[str, List[int]] = {}
    for check in report.get("checks", []) + report.get("smoke", []):
        component = check.get("component") or kind_components.get(check.get("kind"))
        if component and "latency_ms" in check:
            calls.setdefault(component, []).append(check["latency_ms"])
    tasks: Dict[str, List[int]] = {}
    missions: Dict[str, float] = {}
    for run in report.get("e2e", []):
        missions[run["scenario"]] = run["seconds"]
        for call in run["llm_calls"]:
            calls.setdefault(call["component"], []).append(call["latency_ms"])
        for dispatch in run["dispatches"]:
            tasks.setdefault(dispatch["component"], []).append(dispatch["latency_ms"])

    def stats(values: List[int]) -> Dict[str, Any]:
        return {"n": len(values), "mean_ms": round(sum(values) / len(values)), "max_ms": max(values), "total_ms": sum(values)}

    components = ["Planner", "Academic", "Career", "Events", "Services", "Action"]
    return {
        "llm_calls": {c: stats(calls[c]) for c in components if calls.get(c)},
        "agent_tasks": {c: stats(tasks[c]) for c in components if tasks.get(c)},
        "missions_seconds": missions,
    }


def print_e2e(report: Dict[str, Any]) -> None:
    for run in report["e2e"]:
        print(f"\n[E2E {'PASS' if run['pass'] else 'FAIL'}] {run['scenario']}: {run['outcome_label']} "
              f"(mission {run['mission_status']}, {run['seconds']}s) :: {run['goal']}")
        for task in (run.get("plan") or {}).get("tasks", []):
            print(f"      {task['n']}. {task['agent']:<26} deps={task['depends_on']} {task['objective'][:70]}")
        for note in (run.get("plan") or {}).get("unsupported_requests", []):
            print(f"      unsupported: {note[:110]}")
        for dispatch in run["dispatches"]:
            verification = dispatch.get("verification") or {}
            facts = (dispatch.get("result") or {}).get("facts", {})
            print(f"      - task-{dispatch['task_id'].rsplit('-', 1)[-1]} {dispatch['agent']:<24} {dispatch['latency_ms']:>6} ms  "
                  f"verification={verification.get('status', 'n/a')} intent={facts.get('intent', '-')} "
                  f"evidence={len(dispatch.get('evidence', []))} issues={verification.get('issues', dispatch.get('error'))}")
        print(f"      approvals={run['approvals']} tool_calls={run['tool_calls']} "
              f"user_selection_required={run['user_selection_required']}")
        for problem in run["problems"]:
            print(f"      problem: {problem}")
        print(f"      final: {(run['final_result'] or '')[:400]}")


def print_latency(summary: Dict[str, Any]) -> None:
    print("\nLatency (ms):")
    for section, title in (("llm_calls", "LLM calls"), ("agent_tasks", "agent tasks")):
        for component, s in summary[section].items():
            print(f"  {title:<12} {component:<9} n={s['n']:<3} mean={s['mean_ms']:>6} max={s['max_ms']:>6} total={s['total_ms']:>7}")
    for scenario, seconds in summary["missions_seconds"].items():
        print(f"  mission      {scenario:<24} {seconds}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a real LLM provider's structured outputs.")
    parser.add_argument(
        "--provider", choices=sorted(LIVE_PROVIDERS), default=None,
        help="Real provider to check (default: CAMPUSNEXUS_LLM_PROVIDER if it is a real provider, else anthropic).",
    )
    parser.add_argument("--smoke", action="store_true", help="Run only the 5-request smoke checks.")
    parser.add_argument("--e2e", action="store_true", help="Also run each goal end-to-end (more API requests).")
    parser.add_argument(
        "--affected", action="store_true",
        help="Run only the three scenarios the first live E2E run got wrong (intent + missions, with full traces).",
    )
    parser.add_argument("--pause", type=float, default=0.0, help="Seconds to wait between E2E missions (rate limits).")
    parser.add_argument("--out", default=None, help="Write the full JSON report to this path.")
    args = parser.parse_args()

    provider_name = _resolve_provider_name(args.provider)
    try:
        provider = get_llm_provider(provider_name)
    except LLMProviderError as exc:
        print(f"UNAVAILABLE: no live test was run. {exc}")
        sys.exit(2)
    if not provider.is_live:  # defensive: this script must never report mock output as live
        print(f"UNAVAILABLE: provider {provider_name!r} is not a real LLM provider.")
        sys.exit(2)

    mode = "smoke" if args.smoke else "affected" if args.affected else "full"
    report: Dict[str, Any] = {
        "provider": provider.name, "model": provider.model_name, "mode": mode,
        "checks": [], "smoke": [], "e2e": [],
    }
    print(f"Live provider: {provider.name} / {provider.model_name}\n")
    if args.smoke:
        run_smoke(provider, report)
        print_smoke(report)
    elif args.affected:
        check_intents(provider, report, academic_intents=AFFECTED_INTENTS, specialist_intents=[SPECIALIST_INTENTS[1]])
        run_e2e(provider, report, scenarios=AFFECTED_SCENARIOS, pause_seconds=args.pause)
    else:
        check_intents(provider, report)
        check_plans(provider, report)
    if args.e2e and not args.affected:
        run_e2e(provider, report, pause_seconds=args.pause)

    results = report["smoke"] if args.smoke else report["checks"]
    failures: List[Dict[str, Any]] = [c for c in results if not c["pass"]]
    e2e_failures = [run for run in report["e2e"] if not run["pass"]]
    report["latency"] = latency_summary(report)
    for check in report["checks"]:
        label = check.get("query") or check.get("goal")
        detail = check.get("error") or check.get("actual") or f"{len((check.get('plan') or {}).get('tasks', []))} task(s)"
        print(f"[{'PASS' if check['pass'] else 'FAIL'}] {check['kind']:<18} {label[:70]:<70} -> {detail}")
        if check["kind"] == "plan":
            for task in (check.get("plan") or {}).get("tasks", []):
                print(f"         {task['n']}. {task['agent']:<26} deps={task['depends_on']} {task['objective'][:60]}")
            for note in (check.get("plan") or {}).get("unsupported_requests", []):
                print(f"         unsupported: {note[:100]}")
            for error in check.get("validation_errors", []):
                print(f"         validation: {error}")
    print_e2e(report)
    print_latency(report["latency"])

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nFull report: {args.out}")
    # Structured correctness and mission outcomes are reported separately.
    print(f"\n{len(results) - len(failures)}/{len(results)} {'smoke' if args.smoke else 'structured'} checks passed.")
    if report["e2e"]:
        print(f"{len(report['e2e']) - len(e2e_failures)}/{len(report['e2e'])} E2E scenarios reached their correct outcome.")
    sys.exit(1 if failures or e2e_failures else 0)


if __name__ == "__main__":
    main()
