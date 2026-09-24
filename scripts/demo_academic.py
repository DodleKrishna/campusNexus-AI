"""Academic Agent demo runner (Phase 4).

Runs one natural-language academic query for a seeded student through the
full pipeline -- intent classification -> course resolution -> DB facts +
RAG evidence -> deterministic rules -> AcademicVerifier -> final response --
against the real seeded SQLite database and the real Chroma policy store,
and prints every stage for engineering/demo validation.

Uses the offline MockLLMProvider by default (CAMPUSNEXUS_LLM_PROVIDER=mock),
so the full workflow runs with zero credentials. Pass --provider anthropic
(with ANTHROPIC_API_KEY set) to use the real LLM provider instead.

Usage:
    python scripts/demo_academic.py --student STU-DEMO-001 --query "Can I write my OS exam?"
    python scripts/demo_academic.py --student STU-DEMO-001 --query "..." --as-of 2024-06-01
    python scripts/demo_academic.py --student STU-DEMO-001 --query "..." --provider anthropic
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.agents.academic.agent import AcademicAgent
from app.db.session import open_database, create_session_factory
from app.llm.factory import get_llm_provider
from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.retriever import PolicyRetriever
from app.rag.vector_store import PolicyVectorStore
from app.schemas.agent import AgentMessage
from app.schemas.enums import AgentName
from app.services import academic as academic_service
from app.services.knowledge import KnowledgeService


def _print_header(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def _json(value) -> str:
    return json.dumps(value, indent=2, default=str)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CampusNexus Academic Agent demo runner")
    parser.add_argument("--student", required=True, help='Student code, e.g. "STU-DEMO-001"')
    parser.add_argument("--query", required=True, help="Natural-language academic question")
    parser.add_argument("--as-of", default=None, help="Optional ISO date to evaluate policy as of (default: today)")
    parser.add_argument(
        "--provider", default=None, help='LLM provider: "mock" (default) or "anthropic" (requires ANTHROPIC_API_KEY)'
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    engine = open_database()
    session = create_session_factory(engine)()

    config = get_rag_config()
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(
        path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding_provider
    )
    retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
    knowledge_service = KnowledgeService(retriever=retriever)

    llm_provider = get_llm_provider(args.provider)
    agent = AcademicAgent(session=session, knowledge_service=knowledge_service, llm_provider=llm_provider)

    facts = {"student_id": args.student, "query": args.query}
    if args.as_of:
        facts["as_of"] = args.as_of

    message = AgentMessage(
        message_id="demo-msg",
        mission_id="demo-mission",
        task_id="demo-task",
        source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACADEMIC_AGENT,
        objective="answer an academic question",
        facts=facts,
    )

    _print_header("QUERY")
    print(args.query)

    outcome = agent.handle(message)

    _print_header("INTENT")
    print(outcome.agent_result.facts.get("intent"))

    _print_header("STUDENT")
    profile = academic_service.get_student_academic_profile(session, args.student)
    print(_json(profile.model_dump(mode="json")) if profile else "(no student record found)")

    _print_header("COURSE")
    print(_json(outcome.agent_result.facts.get("course_resolution")))

    _print_header("DATABASE FACTS")
    db_facts = {
        key: outcome.agent_result.facts[key]
        for key in ("attendance", "timetable", "exams")
        if key in outcome.agent_result.facts
    }
    print(_json(db_facts) if db_facts else "(none fetched for this intent)")

    _print_header("POLICY EVIDENCE")
    if outcome.agent_result.evidence:
        for ev in outcome.agent_result.evidence:
            print(f"- [{ev.document_id} / {ev.policy_version}] {ev.section}: {ev.snippet}")
    else:
        print("(no evidence retrieved)")

    _print_header("RULE RESULT")
    rule_facts = {
        key: outcome.agent_result.facts[key] for key in ("attendance", "threshold", "eligibility") if key in outcome.agent_result.facts
    }
    print(_json(rule_facts) if rule_facts else "(no deterministic rule invoked for this intent)")

    _print_header("VERIFICATION")
    print(f"status: {outcome.verification.status.value}")
    for check in outcome.verification.checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.name}" + (f" -- {check.detail}" if check.detail else ""))
    if outcome.verification.issues:
        print("issues:")
        for issue in outcome.verification.issues:
            print(f"  - {issue}")

    _print_header("FINAL RESPONSE")
    print(outcome.response_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
