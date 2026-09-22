"""Deterministic RAG retrieval evaluation runner (no LLM judge).

Runs every scenario in eval/rag_scenarios.json through the KnowledgeService
and checks retrieved document_ids against each scenario's expectations.
Exits non-zero if any scenario fails.

Usage:
    python eval/run_rag_eval.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.retriever import PolicyRetriever
from app.rag.vector_store import PolicyVectorStore
from app.services.knowledge import KnowledgeService

SCENARIOS_PATH = Path(__file__).resolve().parent / "rag_scenarios.json"


def load_scenarios() -> List[Dict[str, Any]]:
    return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


def build_knowledge_service() -> KnowledgeService:
    config = get_rag_config()
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(
        path=config.chroma_path,
        collection_name=config.collection_name,
        embedding_provider=embedding_provider,
    )
    retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
    return KnowledgeService(retriever=retriever)


def run_scenario(service: KnowledgeService, scenario: Dict[str, Any]) -> Dict[str, Any]:
    as_of = date.fromisoformat(scenario["as_of"]) if scenario.get("as_of") else None
    filters = scenario.get("filters", {})
    results = service.search(
        scenario["query"],
        as_of=as_of,
        top_k=scenario.get("top_k", 5),
        document_type=filters.get("document_type"),
        department=filters.get("department"),
        audience=filters.get("audience"),
        visibility=filters.get("visibility"),
        document_id=filters.get("document_id"),
        include_historical=filters.get("include_historical", False),
    )
    retrieved_ids = [e.document_id for e in results]
    retrieved_set = set(retrieved_ids)

    if scenario.get("expect_empty", False):
        passed = len(results) == 0
    else:
        expected = scenario.get("expected_document_ids", [])
        forbidden = scenario.get("forbidden_document_ids", [])
        passed = all(doc_id in retrieved_set for doc_id in expected) and not any(
            doc_id in retrieved_set for doc_id in forbidden
        )

    return {"passed": passed, "retrieved_ids": retrieved_ids}


def main() -> int:
    scenarios = load_scenarios()
    service = build_knowledge_service()

    failures = 0
    for scenario in scenarios:
        outcome = run_scenario(service, scenario)
        status = "PASS" if outcome["passed"] else "FAIL"
        if not outcome["passed"]:
            failures += 1
        print(f"{status} {scenario['id']}")
        print(f"     query:    {scenario['query']}")
        print(f"     expected: {scenario.get('expected_document_ids', [])}")
        print(f"     forbidden:{scenario.get('forbidden_document_ids', [])}")
        print(f"     expect_empty: {scenario.get('expect_empty', False)}")
        print(f"     retrieved:{outcome['retrieved_ids']}")

    total = len(scenarios)
    print(f"\n{total - failures}/{total} scenarios passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
