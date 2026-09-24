"""Multi-agent collaboration demo runner (Phase 6).

Runs both Phase 6 flagship missions end-to-end through the real LangGraph
Mission Orchestrator, the real seeded SQLite database, the real Chroma
policy store, and the real Academic/Career/Events/Campus Services agents:

1. The career-preparation mission -- genuine cross-agent collaboration
   (Academic timetable/exams + Career eligibility/skill-gaps both feeding a
   dependent Events Agent conflict-aware workshop search).
2. The campus-services mission -- a single-agent, read-only case/SLA check.

Uses the offline MockLLMProvider by default (CAMPUSNEXUS_LLM_PROVIDER=mock)
for planning and every agent's own intent classification, so the full
workflow runs with zero credentials. Pass --provider anthropic (with
ANTHROPIC_API_KEY set, after `pip install -e ".[dev,llm]"`) for the real LLM.

Usage:
    python scripts/demo_multiagent.py --student STU-DEMO-001
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.agents.academic.agent import AcademicAgent
from app.agents.career.agent import CareerAgent
from app.agents.events.agent import EventsAgent
from app.agents.services.agent import ServicesAgent
from app.db.session import open_database, create_session_factory
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.factory import get_llm_provider
from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.retriever import PolicyRetriever
from app.rag.vector_store import PolicyVectorStore
from app.schemas.enums import AgentName, UserRole
from app.services.context import ContextService
from app.services.knowledge import KnowledgeService

FLAGSHIP_GOAL = (
    "I'm a third-year CSE student interested in AI. Find internships I'm eligible for, identify my skill "
    "gaps, find relevant workshops that don't conflict with my classes, and create a preparation plan."
)
CAMPUS_SERVICES_GOAL = (
    "Check the status of my campus complaints, identify any overdue cases and explain the applicable "
    "grievance procedures."
)


def _print_header(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def _json(value) -> str:
    return json.dumps(value, indent=2, default=str)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CampusNexus multi-agent collaboration demo runner")
    parser.add_argument("--student", default="STU-DEMO-001")
    parser.add_argument("--provider", default=None, help='LLM provider: "mock" (default) or "anthropic"')
    parser.add_argument("--max-replans", type=int, default=2)
    return parser.parse_args()


def build_orchestrator(provider_name, max_replans: int):
    engine = open_database()
    session_factory = create_session_factory(engine)

    config = get_rag_config()
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(
        path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding_provider
    )
    retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
    knowledge_service = KnowledgeService(retriever=retriever)

    llm_provider = get_llm_provider(provider_name)

    registry = AgentRegistry()
    registry.register(
        AgentName.ACADEMIC_AGENT,
        lambda s: AcademicAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm_provider),
    )
    registry.register(
        AgentName.CAREER_AGENT,
        lambda s: CareerAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm_provider),
    )
    registry.register(
        AgentName.EVENTS_OPPORTUNITY_AGENT,
        lambda s: EventsAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm_provider),
    )
    registry.register(
        AgentName.CAMPUS_SERVICES_AGENT,
        lambda s: ServicesAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm_provider),
    )

    orchestrator = MissionOrchestrator(
        session_factory=session_factory, registry=registry, llm_provider=llm_provider, max_replans=max_replans
    )
    return orchestrator, session_factory


def _execution_round(task_id: str, dependencies_by_id: Dict[str, list], memo: Dict[str, int]) -> int:
    """A task's logical dispatch round, derived from its dependency depth --
    not a wall-clock timestamp (the Orchestrator doesn't persist per-round
    timing), but a deterministic, honest representation of *when* in the DAG
    each task actually ran: round 0 = dispatched immediately (no deps),
    round N = dispatched only after every round < N task completed."""
    if task_id in memo:
        return memo[task_id]
    deps = dependencies_by_id.get(task_id, [])
    round_number = 0 if not deps else 1 + max(_execution_round(d, dependencies_by_id, memo) for d in deps)
    memo[task_id] = round_number
    return round_number


def display_mission(final_state: dict, session_factory) -> None:
    _print_header("MISSION GOAL")
    print(final_state["original_goal"])

    _print_header("GENERATED PLAN")
    plan = final_state.get("plan")
    if plan is None:
        print("(no plan -- mission failed before/at planning)")
        return
    for task in plan.tasks:
        deps = f" (depends on: {', '.join(task.dependencies)})" if task.dependencies else " (independent)"
        print(f"- [{task.task_id}] {task.agent.value}: {task.objective}{deps}")

    _print_header("AGENTS INVOLVED")
    agents_used = sorted({task.agent.value for task in plan.tasks})
    print(", ".join(agents_used))

    _print_header("TASK DEPENDENCIES")
    dependencies_by_id = {task.task_id: task.dependencies for task in plan.tasks}
    for task in plan.tasks:
        print(f"- {task.task_id}: depends on {task.dependencies or '(nothing)'}")

    _print_header("EXECUTION TIMELINE")
    memo: Dict[str, int] = {}
    rounds: Dict[int, list] = {}
    for task in plan.tasks:
        r = _execution_round(task.task_id, dependencies_by_id, memo)
        rounds.setdefault(r, []).append(task)
    for round_number in sorted(rounds):
        task_descriptions = [f"{t.task_id} ({t.agent.value})" for t in rounds[round_number]]
        print(f"Round {round_number}: {', '.join(task_descriptions)}")

    _print_header("RETRIEVED EVIDENCE")
    any_evidence = False
    for task_id, result in final_state.get("agent_results", {}).items():
        for ev in result.evidence:
            any_evidence = True
            print(f"- [{task_id}] ({ev.document_id} / {ev.policy_version}) {ev.section}: {ev.snippet[:140]}")
    if not any_evidence:
        print("(no policy evidence retrieved)")

    _print_header("DETERMINISTIC DECISIONS")
    for task_id, result in final_state.get("agent_results", {}).items():
        facts = result.facts
        if "eligibilities" in facts:
            for e in facts["eligibilities"]:
                verdict = "ELIGIBLE" if e["status"] == "eligible" else "NOT ELIGIBLE"
                print(f"- [{task_id}] {e['opportunity']['title']}: {verdict} ({'; '.join(e['blocking_reasons']) or 'no blocking reasons'})")
        if "assessments" in facts:
            for a in facts["assessments"]:
                conflicts = a["timetable_conflicts"] + a["exam_conflicts"]
                conflict_note = f"{len(conflicts)} conflict(s)" if conflicts else "no conflicts"
                print(f"- [{task_id}] {a['event']['title']}: {a['availability']} -- {conflict_note}")
        if "case_assessments" in facts:
            for a in facts["case_assessments"]:
                breach_note = []
                if a["response_breached"]:
                    breach_note.append("response overdue")
                if a["resolution_breached"]:
                    breach_note.append("resolution overdue")
                print(f"- [{task_id}] {a['case']['case_code']}: {'; '.join(breach_note) or 'within SLA'}")
        if "attendance" in facts:
            att = facts["attendance"]
            print(f"- [{task_id}] attendance {att['current_percentage']}% (required {att['required_percentage']}%), eligible_now={att['eligible_now']}")

    _print_header("VERIFICATION RESULTS")
    for task_id, verification in final_state.get("verifications", {}).items():
        print(f"- {task_id}: {verification.status.value}")
        for issue in verification.issues:
            print(f"    issue: {issue}")

    _print_header("FINAL STRUCTURED RECOMMENDATIONS")
    print(final_state.get("final_result") or "(none)")

    _print_header("MISSION STATUS")
    print(final_state["mission_status"].value)

    _print_header("PERSISTED AUDIT TRAIL (Context Service)")
    with session_factory() as session:
        context = ContextService(session)
        for event in context.list_audit_events(final_state["mission_id"]):
            print(f"- {event.event_type}: {event.message}")


def main() -> int:
    args = parse_args()
    orchestrator, session_factory = build_orchestrator(args.provider, args.max_replans)

    print("#" * 70)
    print("# MISSION 1: Career preparation (flagship, multi-agent)")
    print("#" * 70)
    flagship_state = orchestrator.run_mission(
        FLAGSHIP_GOAL, user_id=args.student, user_role=UserRole.STUDENT, student_id=args.student
    )
    display_mission(flagship_state, session_factory)

    print("\n" + "#" * 70)
    print("# MISSION 2: Campus services (single-agent)")
    print("#" * 70)
    services_state = orchestrator.run_mission(
        CAMPUS_SERVICES_GOAL, user_id=args.student, user_role=UserRole.STUDENT, student_id=args.student
    )
    display_mission(services_state, session_factory)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
