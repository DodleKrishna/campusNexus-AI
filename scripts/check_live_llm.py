"""Live LLM validation for CampusNexus (Phase 9).

Exercises the *real* Anthropic provider -- never the mock -- and checks every
structured output with the existing Pydantic schemas and the deterministic
plan validator:

1. Structured academic intents (with course-reference extraction).
2. Structured Career / Events / Campus Services intents.
3. A ``MissionPlan`` for each natural-language demo goal (several phrasings
   each), validated by ``app.graph.validator.validate_plan`` against the full
   five-agent registry: only supported agents, known dependencies, no cycles;
   plus every Action Agent task carrying an allowlisted ``tool_name``.
4. With ``--e2e``: each goal run end-to-end through the real Orchestrator and
   real agents against a throwaway seeded database (never the dev or demo DB).

With no API key (or no SDK) it prints UNAVAILABLE and exits 2 -- it never
fabricates results or falls back to the mock provider. Intent checks compare
against an expected label and count a mismatch as a failure.

Usage:
    python scripts/check_live_llm.py                 # ~20 small requests
    python scripts/check_live_llm.py --e2e           # + full missions (more requests)
    python scripts/check_live_llm.py --out report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.graph.registry import AgentRegistry
from app.graph.validator import validate_plan
from app.llm.base import LLMProviderError
from app.llm.factory import get_llm_provider
from app.schemas.academic import CourseSummary
from app.schemas.enums import AgentName, UserRole

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
    ("What's my timetable this week?", "timetable", None),
    ("When is my DBMS exam?", "exam_schedule", "DBMS"),
]
SPECIALIST_INTENTS = [
    ("career", "Find AI internships I'm eligible for and tell me which skills I'm missing.", "opportunity_discovery"),
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


def check_intents(provider, report: Dict[str, Any]) -> None:
    for query, expected, expected_course in ACADEMIC_INTENTS:
        entry: Dict[str, Any] = {"kind": "academic_intent", "query": query, "expected": expected}
        try:
            result = provider.classify_academic_intent(query, COURSES)
            entry.update(actual=result.intent.value, course_reference=result.raw_course_reference)
            entry["pass"] = result.intent.value == expected and (
                expected_course is None or (result.raw_course_reference or "").lower() == expected_course.lower()
            )
        except LLMProviderError as exc:
            entry.update(error=str(exc), **{"pass": False})
        report["checks"].append(entry)

    for domain, query, expected in SPECIALIST_INTENTS:
        entry = {"kind": f"{domain}_intent", "query": query, "expected": expected}
        classify = getattr(provider, f"classify_{domain}_intent")
        try:
            actual = classify(query).intent.value
            entry.update(actual=actual, **{"pass": actual == expected})
        except LLMProviderError as exc:
            entry.update(error=str(exc), **{"pass": False})
        report["checks"].append(entry)


def check_plans(provider, report: Dict[str, Any]) -> None:
    registry = _registry_for_validation()
    for category, goals in GOALS.items():
        for n, goal in enumerate(goals, start=1):
            mission_id = f"live-{category}-{n}"
            entry: Dict[str, Any] = {"kind": "plan", "category": category, "goal": goal}
            try:
                plan = provider.plan_mission(mission_id, goal, supported_agents=ALL_AGENTS)
            except LLMProviderError as exc:
                entry.update(error=str(exc), **{"pass": False})
                report["checks"].append(entry)
                continue
            validation = validate_plan(plan, registry, expected_mission_id=mission_id)
            action_errors = [
                f"{t.task_id}: missing/unknown tool_name {t.constraints.get('tool_name')!r}"
                for t in plan.tasks
                if t.agent == AgentName.ACTION_AGENT and t.constraints.get("tool_name") not in ACTION_TOOLS
            ]
            entry["plan"] = _plan_view(plan)
            entry["validation_errors"] = validation.errors + action_errors
            if category == "unsupported":
                # A decline is the correct outcome: zero tasks plus an explanation.
                entry["pass"] = not plan.tasks and bool(plan.unsupported_requests)
            else:
                entry["pass"] = bool(plan.tasks) and validation.is_valid and not action_errors
            report["checks"].append(entry)


def run_e2e(provider, report: Dict[str, Any]) -> None:
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

    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda s: AcademicAgent(session=s, knowledge_service=knowledge, llm_provider=provider))
    registry.register(AgentName.CAREER_AGENT, lambda s: CareerAgent(session=s, knowledge_service=knowledge, llm_provider=provider))
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, lambda s: EventsAgent(session=s, knowledge_service=knowledge, llm_provider=provider))
    registry.register(AgentName.CAMPUS_SERVICES_AGENT, lambda s: ServicesAgent(session=s, knowledge_service=knowledge, llm_provider=provider))
    registry.register(AgentName.ACTION_AGENT, lambda s: ActionAgent(session=s, knowledge_service=knowledge, tool_gateway=gateway))
    orchestrator = MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=provider)

    for category, goals in GOALS.items():
        goal = goals[0]
        started = time.perf_counter()
        state = orchestrator.run_mission(goal, user_id="STU-DEMO-001", user_role=UserRole.STUDENT, student_id="STU-DEMO-001")
        plan = state.get("plan")
        report["e2e"].append({
            "category": category,
            "goal": goal,
            "seconds": round(time.perf_counter() - started, 1),
            "mission_status": state["mission_status"].value,
            "plan": _plan_view(plan) if plan is not None else None,
            "task_status": {k: v.value for k, v in state.get("task_status", {}).items()},
            "evidence_counts": {k: len(v.evidence) for k, v in state.get("agent_results", {}).items()},
            "errors": state.get("errors", []),
            "final_result": state.get("final_result"),
        })


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the real Anthropic provider's structured outputs.")
    parser.add_argument("--e2e", action="store_true", help="Also run each goal end-to-end (more API requests).")
    parser.add_argument("--out", default=None, help="Write the full JSON report to this path.")
    args = parser.parse_args()

    try:
        provider = get_llm_provider("anthropic")
    except LLMProviderError as exc:
        print(f"UNAVAILABLE: no live test was run. {exc}")
        sys.exit(2)

    report: Dict[str, Any] = {"provider": provider.name, "model": provider.model_name, "checks": [], "e2e": []}
    print(f"Live provider: {provider.name} / {provider.model_name}\n")
    check_intents(provider, report)
    check_plans(provider, report)
    if args.e2e:
        run_e2e(provider, report)

    failures: List[Dict[str, Any]] = [c for c in report["checks"] if not c["pass"]]
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
    for run in report["e2e"]:
        print(f"\n[E2E] {run['category']}: {run['mission_status']} in {run['seconds']}s :: {run['goal']}")
        print(f"      {(run['final_result'] or '')[:400]}")

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nFull report: {args.out}")
    print(f"\n{len(report['checks']) - len(failures)}/{len(report['checks'])} structured checks passed.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
