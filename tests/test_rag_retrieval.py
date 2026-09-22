"""Tests for hybrid retrieval: metadata filters, active-policy dating, ranking,
citation integrity, no-evidence behavior, and conflict preservation.

Uses the session-scoped ``rag_retriever`` fixture (tests/conftest.py), which
ingests the real data/policies corpus into a throwaway Chroma collection with
the offline DeterministicHashEmbedding -- no network, no LLM.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.rag.documents import load_policy_documents
from app.rag.retriever import PolicyRetriever, RetrievalQuery

TODAY = date(2026, 9, 22)
HISTORICAL_DATE = date(2024, 1, 15)


# ---------------------------------------------------------------------------
# Required retrieval scenarios (Phase 3 spec section 16)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, filters, expected_document_id",
    [
        ("What are the eligibility requirements to appear for end semester examinations?", {}, "exam-regulations"),
        (
            "Who is eligible for campus placement and how many offers can a student accept?",
            {},
            "placement-policy",
        ),
        (
            "Can I get academic credit for an internship and how does it affect my attendance?",
            {},
            "internship-policy",
        ),
        (
            "What is the SLA response and resolution time for an urgent hostel complaint?",
            {},
            "grievance-sla-policy",
        ),
        ("How do I request a hostel room reallocation or report a maintenance issue?", {}, "hostel-policy"),
        ("What CGPA is required to be eligible for the merit scholarship?", {}, "scholarship-policy"),
        (
            "How many books can I borrow from the library and what is the fine for a late return?",
            {},
            "library-policy",
        ),
    ],
)
def test_required_scenarios_retrieve_expected_document(
    rag_retriever: PolicyRetriever, query: str, filters: dict, expected_document_id: str
) -> None:
    results = rag_retriever.retrieve(RetrievalQuery(query=query, top_k=5, **filters))
    assert expected_document_id in {r.document_id for r in results}


def test_event_registration_general_excludes_department_circular(rag_retriever: PolicyRetriever) -> None:
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="How do I register for a campus event and what happens if it is full?",
            department="ALL",
            top_k=5,
        )
    )
    document_ids = {r.document_id for r in results}
    assert "event-policy" in document_ids
    assert "cse-event-circular-2025" not in document_ids


def test_no_supporting_document_returns_no_evidence(rag_retriever: PolicyRetriever) -> None:
    results = rag_retriever.retrieve(
        RetrievalQuery(query="What is the weather forecast for this weekend's cricket match?", top_k=5)
    )
    assert results == []


# ---------------------------------------------------------------------------
# Active vs historical policy filtering
# ---------------------------------------------------------------------------


def test_current_attendance_query_resolves_to_active_version(rag_retriever: PolicyRetriever) -> None:
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="What is the minimum attendance percentage required to sit for exams?",
            as_of=TODAY,
            document_type="attendance_policy",
            top_k=5,
        )
    )
    document_ids = {r.document_id for r in results}
    assert "attendance-policy-v2" in document_ids
    assert "attendance-policy-v1" not in document_ids
    assert all(r.policy_version == "v2" for r in results)


def test_historical_attendance_query_resolves_to_prior_version(rag_retriever: PolicyRetriever) -> None:
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="What is the minimum attendance percentage required to sit for exams?",
            as_of=HISTORICAL_DATE,
            document_type="attendance_policy",
            top_k=5,
        )
    )
    document_ids = {r.document_id for r in results}
    assert "attendance-policy-v1" in document_ids
    assert "attendance-policy-v2" not in document_ids
    assert all(r.policy_version == "v1" for r in results)


def test_document_id_scoped_lookup_bypasses_active_filter(rag_retriever: PolicyRetriever) -> None:
    """A superseded version must remain explicitly retrievable (auditability)."""
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="minimum attendance requirement",
            as_of=TODAY,
            document_id="attendance-policy-v1",
            top_k=5,
        )
    )
    assert results
    assert all(r.document_id == "attendance-policy-v1" for r in results)


def test_no_as_of_does_not_apply_date_filtering(rag_retriever: PolicyRetriever) -> None:
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="minimum attendance requirement", document_type="attendance_policy", top_k=10
        )
    )
    versions = {r.policy_version for r in results}
    assert versions == {"v1", "v2"}


# ---------------------------------------------------------------------------
# Visibility filtering
# ---------------------------------------------------------------------------


def test_student_visible_query_never_returns_admin_only_document(rag_retriever: PolicyRetriever) -> None:
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="What is the internal SOP for escalating a breached SLA case?",
            visibility="public",
            top_k=5,
        )
    )
    assert all(r.visibility == "public" for r in results)
    assert "internal-case-escalation-sop" not in {r.document_id for r in results}


def test_admin_scoped_query_can_retrieve_admin_only_document(rag_retriever: PolicyRetriever) -> None:
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="What is the internal SOP for escalating a breached SLA case?",
            visibility="admin_only",
            top_k=5,
        )
    )
    document_ids = {r.document_id for r in results}
    assert "internal-case-escalation-sop" in document_ids


# ---------------------------------------------------------------------------
# Deliberate conflict / ambiguity preservation
# ---------------------------------------------------------------------------


def test_department_scoped_query_surfaces_cse_circular(rag_retriever: PolicyRetriever) -> None:
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="How many hours in advance must I register for a CSE department technical event?",
            department="CSE",
            top_k=5,
        )
    )
    assert "cse-event-circular-2025" in {r.document_id for r in results}


def test_unscoped_event_query_preserves_both_conflicting_documents(rag_retriever: PolicyRetriever) -> None:
    """A department-unscoped query must surface both the general policy and the
    CSE-specific circular -- the retrieval layer must not silently pick one,
    so a future Verifier can detect and flag the discrepancy."""
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="What is the event registration deadline procedure at Meridian University?",
            top_k=8,
        )
    )
    document_ids = {r.document_id for r in results}
    assert "event-policy" in document_ids
    assert "cse-event-circular-2025" in document_ids


# ---------------------------------------------------------------------------
# Other metadata filters
# ---------------------------------------------------------------------------


def test_document_type_filter_restricts_results(rag_retriever: PolicyRetriever) -> None:
    results = rag_retriever.retrieve(
        RetrievalQuery(
            query="How many books can a student borrow and what is the loan period?",
            document_type="library_policy",
            top_k=10,
        )
    )
    assert results
    assert all(r.document_type == "library_policy" for r in results)


# ---------------------------------------------------------------------------
# Citation integrity
# ---------------------------------------------------------------------------


def test_retrieved_text_is_exact_substring_of_source_document(rag_retriever: PolicyRetriever, policy_dir: Path) -> None:
    documents = {d.document_id: d for d in load_policy_documents(policy_dir)}
    results = rag_retriever.retrieve(
        RetrievalQuery(query="minimum attendance requirement", document_type="attendance_policy", top_k=5)
    )
    assert results
    for result in results:
        document = documents[result.document_id]
        raw_text = Path(policy_dir).parent.parent.joinpath(document.source_path).read_text(encoding="utf-8")
        assert result.text in raw_text
        assert result.source == document.source_path


def test_get_document_chunks_matches_source_document(rag_retriever: PolicyRetriever, policy_dir: Path) -> None:
    document = next(d for d in load_policy_documents(policy_dir) if d.document_id == "hostel-policy")
    chunks = rag_retriever.get_document_chunks("hostel-policy")

    assert chunks
    assert all(c.document_id == "hostel-policy" for c in chunks)
    # Every chunk came from a real section, in the document's original order.
    assert [c.section for c in chunks] == [s.heading for s in document.sections]
