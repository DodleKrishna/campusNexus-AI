"""Mission Orchestrator demo runner (Phase 5).

Runs a natural-language student goal end-to-end through the new LangGraph
Mission Orchestrator: goal -> structured MissionPlan -> validated DAG ->
dispatched Academic Agent tasks (real seeded DB + real Chroma policy store)
-> AcademicVerifier-checked results -> persisted Context Service state ->
final mission result. Prints every stage for engineering/demo validation.

Uses the offline MockLLMProvider by default (CAMPUSNEXUS_LLM_PROVIDER=mock)
for both planning and the Academic Agent's own intent classification, so the
full workflow runs with zero credentials. Pass --provider anthropic (with
ANTHROPIC_API_KEY set, after `pip install -e ".[dev,llm]"`) to use the real
LLM provider instead.

Usage:
    python scripts/demo_mission.py --student STU-DEMO-001 \
        --goal "Check my Operating Systems attendance, determine whether I currently meet the attendance requirement, and explain how many classes I need to attend to reach the required attendance."

    python scripts/demo_mission.py --resume <mission_id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.agents.academic.agent import AcademicAgent
from app.db.session import create_db_engine, create_session_factory
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


def _print_header(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def _json(value) -> str:
    return json.dumps(value, indent=2, default=str)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CampusNexus Mission Orchestrator demo runner")
    parser.add_argument("--student", default="STU-DEMO-001", help='Student code, e.g. "STU-DEMO-001"')
    parser.add_argument("--goal", default=None, help="Natural-language student goal")
    parser.add_argument("--resume", default=None, metavar="MISSION_ID", help="Resume an existing mission instead of starting a new one")
    parser.add_argument("--max-replans", type=int, default=2, help="Bounded replanning budget (default: 2)")
    parser.add_argument(
        "--provider", default=None, help='LLM provider: "mock" (default) or "anthropic" (requires ANTHROPIC_API_KEY)'
    )
    args = parser.parse_args()
    if not args.resume and not args.goal:
        parser.error("either --goal or --resume is required")
    return args


def build_orchestrator(provider_name: str | None, max_replans: int):
    engine = create_db_engine()
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
        lambda session: AcademicAgent(session=session, knowledge_service=knowledge_service, llm_provider=llm_provider),
    )

    orchestrator = MissionOrchestrator(
        session_factory=session_factory, registry=registry, llm_provider=llm_provider, max_replans=max_replans
    )
    return orchestrator, session_factory


def display(final_state: dict, session_factory) -> None:
    _print_header("MISSION ID")
    print(final_state["mission_id"])

    _print_header("STUDENT GOAL")
    print(final_state["original_goal"])

    _print_header("MISSION PLAN")
    plan = final_state.get("plan")
    if plan is None:
        print("(no plan -- mission failed before/at planning)")
    else:
        for task in plan.tasks:
            deps = f" (depends on: {', '.join(task.dependencies)})" if task.dependencies else ""
            print(f"- [{task.task_id}] {task.agent.value}: {task.objective}{deps}")

    _print_header("TASK EXECUTION")
    task_status = final_state.get("task_status", {})
    if not task_status:
        print("(no tasks were dispatched)")
    for task_id, status in task_status.items():
        print(f"- {task_id}: {status.value}")

    _print_header("AGENT RESULTS")
    agent_results = final_state.get("agent_results", {})
    if not agent_results:
        print("(no agent results)")
    for task_id, result in agent_results.items():
        print(f"- {task_id} ({result.agent.value}, {result.status.value}):")
        print(f"    {_json(result.facts)}")

    _print_header("POLICY EVIDENCE")
    any_evidence = False
    for task_id, result in agent_results.items():
        for ev in result.evidence:
            any_evidence = True
            print(f"- [{task_id}] ({ev.document_id} / {ev.policy_version}) {ev.section}: {ev.snippet}")
    if not any_evidence:
        print("(no policy evidence retrieved)")

    _print_header("VERIFICATION")
    verifications = final_state.get("verifications", {})
    for task_id, verification in verifications.items():
        print(f"- {task_id}: {verification.status.value}")
        for issue in verification.issues:
            print(f"    issue: {issue}")

    _print_header("FINAL RESULT")
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

    if args.resume:
        final_state = orchestrator.resume_mission(args.resume)
    else:
        final_state = orchestrator.run_mission(
            args.goal, user_id=args.student, user_role=UserRole.STUDENT, student_id=args.student
        )

    display(final_state, session_factory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
