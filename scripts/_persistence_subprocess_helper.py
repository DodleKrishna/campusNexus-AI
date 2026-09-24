"""Test-only helper: two subcommands run in genuinely separate OS processes
by tests/test_persistence_subprocess.py, proving mission pause/approve/resume
survives a real process boundary -- not just two MissionOrchestrator
instances sharing one Python interpreter (see tests/test_action_orchestration.py
for that same-process variant).

Reads CAMPUSNEXUS_DB_PATH / CAMPUSNEXUS_VECTOR_STORE_PATH /
CAMPUSNEXUS_EMBEDDING_PROVIDER / CAMPUSNEXUS_LLM_PROVIDER from the
environment (set by the calling test), exactly like every other script in
this repo. Prints exactly one JSON line to stdout.

Usage:
    python scripts/_persistence_subprocess_helper.py propose --student STU-DEMO-001
    python scripts/_persistence_subprocess_helper.py approve-and-resume --mission-id <id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.agents.action.agent import ActionAgent
from app.agents.events.agent import EventsAgent
from app.db.repositories.missions import get_latest_approval_for_step
from app.db.session import open_database, create_session_factory
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.factory import get_llm_provider
from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.retriever import PolicyRetriever
from app.rag.vector_store import PolicyVectorStore
from app.schemas.enums import AgentName, ApprovalStatus, UserRole
from app.services.approval_gate import ApprovalGate
from app.services.knowledge import KnowledgeService
from app.tools.build import build_default_tool_registry

REGISTER_GOAL = (
    "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)


def _build_orchestrator() -> Tuple[MissionOrchestrator, object]:
    engine = open_database()
    session_factory = create_session_factory(engine)

    config = get_rag_config()
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(
        path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding_provider
    )
    retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
    knowledge_service = KnowledgeService(retriever=retriever)

    llm_provider = get_llm_provider(None)
    tool_gateway = build_default_tool_registry()
    registry = AgentRegistry()
    registry.register(
        AgentName.EVENTS_OPPORTUNITY_AGENT,
        lambda s: EventsAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm_provider),
    )
    registry.register(
        AgentName.ACTION_AGENT, lambda s: ActionAgent(session=s, knowledge_service=knowledge_service, tool_gateway=tool_gateway)
    )

    orchestrator = MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm_provider)
    return orchestrator, session_factory


def cmd_propose(args: argparse.Namespace) -> None:
    orchestrator, _ = _build_orchestrator()
    final = orchestrator.run_mission(REGISTER_GOAL, user_id=args.student, user_role=UserRole.STUDENT, student_id=args.student)
    print(json.dumps({"mission_id": final["mission_id"], "mission_status": final["mission_status"].value}))


def cmd_approve_and_resume(args: argparse.Namespace) -> None:
    """Approve the pending approval if one is still PENDING, then resume.

    A repeated invocation against an already-APPROVED (or otherwise
    resolved) approval skips the decide() call -- ApprovalGate correctly
    refuses to silently re-decide it -- and goes straight to resume_mission,
    which is itself a safe, idempotent no-op for an already-completed
    mission (see tests/test_orchestrator_persistence.py's same-process
    precedent). This is what makes repeated resumption safe to demonstrate.
    """
    orchestrator, session_factory = _build_orchestrator()
    task_id = f"{args.mission_id}-task-2"
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
        if approval is None:
            print(json.dumps({"error": f"no approval found for step {task_id!r}"}))
            return
        if approval.status == ApprovalStatus.PENDING:
            ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-subprocess")

    final = orchestrator.resume_mission(args.mission_id)
    print(json.dumps({"mission_status": final["mission_status"].value}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    propose_parser = subparsers.add_parser("propose")
    propose_parser.add_argument("--student", default="STU-DEMO-001")
    propose_parser.set_defaults(func=cmd_propose)

    resume_parser = subparsers.add_parser("approve-and-resume")
    resume_parser.add_argument("--mission-id", required=True)
    resume_parser.set_defaults(func=cmd_approve_and_resume)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
