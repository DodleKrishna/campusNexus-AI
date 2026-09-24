"""Test-only helper (Phase 13): candidate selection across real process boundaries.

Each subcommand runs in its own OS process, driven by
tests/test_phase13_api_ui.py, so candidates and the student's selection are
proven to live in the database -- not in any process's memory. Reads the same
CAMPUSNEXUS_* environment variables as every other script. Prints exactly
one JSON line to stdout.

Usage:
    python scripts/_selection_subprocess_helper.py discover --student STU-DEMO-001
    python scripts/_selection_subprocess_helper.py select --mission-id <id> --event-id 10
    python scripts/_selection_subprocess_helper.py approve-and-resume --mission-id <id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.api.main import _build_registry  # noqa: E402
from app.db.repositories.missions import get_latest_approval_for_step  # noqa: E402
from app.db.session import create_db_engine, create_session_factory  # noqa: E402
from app.graph.orchestrator import MissionOrchestrator  # noqa: E402
from app.llm.factory import get_llm_provider  # noqa: E402
from app.rag.config import get_rag_config  # noqa: E402
from app.rag.embeddings import get_embedding_provider  # noqa: E402
from app.rag.retriever import PolicyRetriever  # noqa: E402
from app.rag.vector_store import PolicyVectorStore  # noqa: E402
from app.schemas.enums import ApprovalStatus, UserRole  # noqa: E402
from app.services import target_selection  # noqa: E402
from app.services.approval_gate import ApprovalGate  # noqa: E402
from app.services.context import ContextService  # noqa: E402
from app.tools.build import build_default_tool_registry  # noqa: E402

GOAL = "Find a suitable event, check my schedule and prepare my registration."


def _build():
    session_factory = create_session_factory(create_db_engine())
    config = get_rag_config()
    embedding = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    store = PolicyVectorStore(path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding)
    from app.services.knowledge import KnowledgeService

    knowledge = KnowledgeService(retriever=PolicyRetriever(vector_store=store, embedding_provider=embedding))
    llm = get_llm_provider(None)
    registry = _build_registry(knowledge, llm, build_default_tool_registry())
    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm), session_factory


def cmd_discover(args: argparse.Namespace) -> dict:
    orchestrator, session_factory = _build()
    final = orchestrator.run_mission(GOAL, user_id=args.student, user_role=UserRole.STUDENT, student_id=args.student)
    with session_factory() as session:
        candidates = target_selection.list_candidates(session, final["mission_id"])
    return {
        "mission_id": final["mission_id"],
        "mission_status": final["mission_status"].value,
        "candidates": {str(c.resource_id): c.assessment.status.value for c in candidates},
    }


def cmd_select(args: argparse.Namespace) -> dict:
    orchestrator, session_factory = _build()
    with session_factory() as session:
        before = {str(c.resource_id): c.assessment.status.value for c in target_selection.list_candidates(session, args.mission_id)}
    outcome = orchestrator.select_target(
        args.mission_id, tool_name="register_event", resource_type="event", resource_id=args.event_id,
        selected_by=args.student,
    )
    with session_factory() as session:
        mission = ContextService(session).get_mission(args.mission_id)
        status = mission.status.value
    return {"result": outcome.result.value, "candidates_seen": before, "mission_status": status}


def cmd_approve_and_resume(args: argparse.Namespace) -> dict:
    orchestrator, session_factory = _build()
    with session_factory() as session:
        selections = ContextService(session).list_active_target_selections(args.mission_id)
        selection = next(iter(selections.values()))
        approval = get_latest_approval_for_step(session, selection.step_id)
        if approval.status == ApprovalStatus.PENDING:
            ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-subprocess")
    final = orchestrator.resume_mission(args.mission_id)
    return {"mission_status": final["mission_status"].value, "selected_event_id": selection.resource_id}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover")
    discover.add_argument("--student", default="STU-DEMO-001")
    discover.set_defaults(func=cmd_discover)
    select_parser = sub.add_parser("select")
    select_parser.add_argument("--mission-id", required=True)
    select_parser.add_argument("--event-id", type=int, required=True)
    select_parser.add_argument("--student", default="STU-DEMO-001")
    select_parser.set_defaults(func=cmd_select)
    resume = sub.add_parser("approve-and-resume")
    resume.add_argument("--mission-id", required=True)
    resume.set_defaults(func=cmd_approve_and_resume)
    args = parser.parse_args()
    print(json.dumps(args.func(args)))


if __name__ == "__main__":
    main()
