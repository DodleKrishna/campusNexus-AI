"""Deterministic Academic Agent evaluation (no LLM judge, no live LLM).

Runs every scenario in eval/academic_scenarios.json through the real
AcademicAgent -- real seeded SQLite DB, real Chroma policy store, but the
MockLLMProvider (CLAUDE.md/spec section 13: automated evaluation must not
depend on live LLM output) -- and checks the resulting AgentResult /
VerificationResult against each scenario's expectations. Exits non-zero if
any scenario fails.

Two of the sixteen scenarios in the Phase 4 spec are intentionally not
represented here:

- "exactly at threshold": no seeded course sits at exactly the active
  policy's threshold, and perturbing the approved Phase 2 seed data to add
  one was avoided. Covered instead by
  tests/test_attendance_rule.py::test_exactly_at_threshold_is_eligible.
- "conflicting/ambiguous policy evidence": the seeded attendance policy
  corpus has two versions with contiguous, non-overlapping effective
  windows, so there's no live conflict to query against without editing the
  approved Phase 3 corpus. Covered instead by
  tests/test_policy_threshold.py::test_ambiguous_threshold_needs_review.

Usage:
    python eval/run_academic_eval.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

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
from app.services.knowledge import KnowledgeService

SCENARIOS_PATH = Path(__file__).resolve().parent / "academic_scenarios.json"


def load_scenarios() -> list[Dict[str, Any]]:
    return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


def build_agent() -> AcademicAgent:
    engine = open_database()
    session = create_session_factory(engine)()

    config = get_rag_config()
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(
        path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding_provider
    )
    retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
    knowledge_service = KnowledgeService(retriever=retriever)

    llm_provider = get_llm_provider("mock")
    return AcademicAgent(session=session, knowledge_service=knowledge_service, llm_provider=llm_provider)


def run_scenario(agent: AcademicAgent, scenario: Dict[str, Any]) -> Dict[str, Any]:
    facts: Dict[str, Any] = {"student_id": scenario["student_id"], "query": scenario["query"]}
    if scenario.get("as_of"):
        facts["as_of"] = scenario["as_of"]

    message = AgentMessage(
        message_id=f"eval-{scenario['id']}",
        mission_id="eval-mission",
        task_id=f"eval-{scenario['id']}-task",
        source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACADEMIC_AGENT,
        objective="academic evaluation",
        facts=facts,
    )
    outcome = agent.handle(message)
    expected = scenario["expected"]

    actual: Dict[str, Any] = {
        "intent": outcome.agent_result.facts.get("intent"),
        "verification_status": outcome.verification.status.value,
    }
    checks = [
        actual["intent"] == expected["intent"],
        actual["verification_status"] == expected["verification_status"],
    ]

    if "course_code" in expected:
        actual["course_code"] = outcome.agent_result.facts.get("course_resolution", {}).get("course_code")
        checks.append(actual["course_code"] == expected["course_code"])

    attendance = outcome.agent_result.facts.get("attendance") or {}
    if "eligible_now" in expected:
        actual["eligible_now"] = attendance.get("eligible_now")
        checks.append(actual["eligible_now"] == expected["eligible_now"])
    if "current_percentage" in expected:
        actual["current_percentage"] = attendance.get("current_percentage")
        checks.append(actual["current_percentage"] == expected["current_percentage"])
    if "classes_needed_to_reach_threshold" in expected:
        actual["classes_needed_to_reach_threshold"] = attendance.get("classes_needed_to_reach_threshold")
        checks.append(actual["classes_needed_to_reach_threshold"] == expected["classes_needed_to_reach_threshold"])

    if "threshold_document_id" in expected:
        threshold = outcome.agent_result.facts.get("threshold") or {}
        actual["threshold_document_id"] = threshold.get("document_id")
        checks.append(actual["threshold_document_id"] == expected["threshold_document_id"])

    if expected.get("expect_empty_evidence"):
        actual["evidence_count"] = len(outcome.agent_result.evidence)
        checks.append(actual["evidence_count"] == 0)

    return {"passed": all(checks), "actual": actual, "response": outcome.response_text}


def main() -> int:
    scenarios = load_scenarios()
    agent = build_agent()

    failures = 0
    for scenario in scenarios:
        outcome = run_scenario(agent, scenario)
        status = "PASS" if outcome["passed"] else "FAIL"
        if not outcome["passed"]:
            failures += 1
        print(f"{status} {scenario['id']}: {scenario['description']}")
        print(f"     query:    {scenario['query']}")
        print(f"     expected: {scenario['expected']}")
        print(f"     actual:   {outcome['actual']}")

    total = len(scenarios)
    print(f"\n{total - failures}/{total} scenarios passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
