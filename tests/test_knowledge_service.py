"""Tests for KnowledgeService: retrieval -> Evidence conversion at the service boundary."""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.schemas.evidence import Evidence
from app.services.knowledge import KnowledgeService

TODAY = date(2026, 9, 22)
HISTORICAL_DATE = date(2024, 1, 15)


def test_search_returns_evidence_instances(knowledge_service: KnowledgeService) -> None:
    results = knowledge_service.search(
        "What CGPA is required to be eligible for the merit scholarship?", top_k=5
    )
    assert results
    assert all(isinstance(r, Evidence) for r in results)

    top = results[0]
    assert top.document_id == "scholarship-policy"
    assert top.evidence_id.startswith("ev-scholarship-policy::chunk::")
    assert top.source == "data/policies/scholarship_policy.md"
    assert top.section is not None
    assert 0.0 <= top.relevance_score <= 1.0
    assert isinstance(top.effective_from, datetime)
    assert top.effective_from.tzinfo == timezone.utc


def test_search_snippet_is_grounded_not_fabricated(knowledge_service: KnowledgeService) -> None:
    results = knowledge_service.search(
        "How many books can I borrow from the library?", document_type="library_policy", top_k=3
    )
    assert results
    for evidence in results:
        assert evidence.document_id == "library-policy"
        # The snippet is exactly the retrieved chunk's source text -- never an
        # LLM paraphrase -- so it must contain real policy vocabulary from
        # that specific document.
        assert "librar" in evidence.snippet.lower() or "book" in evidence.snippet.lower()


def test_search_blank_query_returns_empty(knowledge_service: KnowledgeService) -> None:
    assert knowledge_service.search("", top_k=5) == []
    assert knowledge_service.search("   ", top_k=5) == []


def test_search_unsupported_query_returns_empty_never_fabricated(knowledge_service: KnowledgeService) -> None:
    results = knowledge_service.search("What is the weather forecast for this weekend's cricket match?", top_k=5)
    assert results == []


def test_search_respects_visibility_filter(knowledge_service: KnowledgeService) -> None:
    public_results = knowledge_service.search(
        "What is the internal SOP for escalating a breached SLA case?", visibility="public", top_k=5
    )
    assert "internal-case-escalation-sop" not in {e.document_id for e in public_results}

    admin_results = knowledge_service.search(
        "What is the internal SOP for escalating a breached SLA case?", visibility="admin_only", top_k=5
    )
    assert "internal-case-escalation-sop" in {e.document_id for e in admin_results}


def test_get_active_policy_returns_current_version_only(knowledge_service: KnowledgeService) -> None:
    evidence = knowledge_service.get_active_policy("attendance_policy", as_of=TODAY)
    assert evidence
    assert all(e.policy_version == "v2" for e in evidence)
    assert all(e.document_id == "attendance-policy-v2" for e in evidence)


def test_get_active_policy_returns_historical_version_for_past_date(knowledge_service: KnowledgeService) -> None:
    evidence = knowledge_service.get_active_policy("attendance_policy", as_of=HISTORICAL_DATE)
    assert evidence
    assert all(e.policy_version == "v1" for e in evidence)
    assert all(e.document_id == "attendance-policy-v1" for e in evidence)


def test_get_active_policy_defaults_as_of_to_today(knowledge_service: KnowledgeService) -> None:
    evidence = knowledge_service.get_active_policy("attendance_policy")
    assert evidence
    assert all(e.policy_version == "v2" for e in evidence)


def test_get_document_evidence_returns_full_document_in_order(knowledge_service: KnowledgeService) -> None:
    evidence = knowledge_service.get_document_evidence("hostel-policy")
    assert evidence
    assert all(e.document_id == "hostel-policy" for e in evidence)
    sections = [e.section for e in evidence]
    assert sections == [
        "Room Allocation and Reallocation",
        "Maintenance Requests",
        "Hostel Rules",
        "Fee and Vacancy",
    ]


def test_get_document_evidence_unknown_document_returns_empty(knowledge_service: KnowledgeService) -> None:
    assert knowledge_service.get_document_evidence("does-not-exist") == []


def test_evidence_effective_dates_are_timezone_aware(knowledge_service: KnowledgeService) -> None:
    evidence = knowledge_service.get_active_policy("attendance_policy", as_of=TODAY)
    assert evidence
    for e in evidence:
        assert e.effective_from.tzinfo is not None
        if e.effective_to is not None:
            assert e.effective_to.tzinfo is not None


# ---------------------------------------------------------------------------
# RAG evaluation scenarios, run deterministically (no LLM judge, no network).
# ---------------------------------------------------------------------------


def test_eval_scenarios_pass_against_deterministic_knowledge_service(knowledge_service: KnowledgeService) -> None:
    """The scenario file in eval/rag_scenarios.json must pass against the same
    offline KnowledgeService the rest of this test module uses -- this keeps
    the eval dataset honest as retrieval logic changes, independent of
    whichever embedding provider is configured for the live demo."""
    import json
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    eval_dir = repo_root / "eval"
    if str(eval_dir) not in sys.path:
        sys.path.insert(0, str(eval_dir))
    from run_rag_eval import run_scenario  # type: ignore[import-not-found]

    scenarios = json.loads((eval_dir / "rag_scenarios.json").read_text(encoding="utf-8"))
    assert len(scenarios) >= 15

    failures = []
    for scenario in scenarios:
        outcome = run_scenario(knowledge_service, scenario)
        if not outcome["passed"]:
            failures.append((scenario["id"], outcome["retrieved_ids"]))

    assert not failures, f"RAG eval scenarios failed: {failures}"
