"""Unit tests for deterministic threshold extraction (app/rules/policy_threshold.py).

Uses synthetic Evidence fixtures rather than the live corpus so the
AMBIGUOUS/NOT_FOUND paths can be exercised without editing the approved
Phase 3 policy documents (the real corpus has no live conflicting-policy
scenario -- see eval/run_academic_eval.py's module docstring).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.rules.policy_threshold import extract_attendance_threshold
from app.schemas.academic import ThresholdExtractionStatus
from app.schemas.evidence import Evidence


def _evidence(
    *,
    evidence_id: str,
    document_id: str,
    snippet: str,
    section: str | None = "Minimum Attendance Requirement",
    policy_version: str = "v2",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        document_id=document_id,
        title="Attendance Policy",
        source=f"data/policies/{document_id}.md",
        snippet=snippet,
        section=section,
        policy_version=policy_version,
        effective_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
    )


def test_extracts_threshold_from_known_section() -> None:
    evidence = [
        _evidence(
            evidence_id="ev-1",
            document_id="attendance-policy-v2",
            snippet="Students must maintain a minimum of 75% attendance in each enrolled course.",
        )
    ]
    result = extract_attendance_threshold(evidence)
    assert result.status == ThresholdExtractionStatus.OK
    assert result.required_percentage == Decimal("75")
    assert result.document_id == "attendance-policy-v2"
    assert result.policy_version == "v2"
    assert result.section == "Minimum Attendance Requirement"
    assert result.evidence_id == "ev-1"


def test_fallback_pattern_matches_alternate_phrasing() -> None:
    evidence = [
        _evidence(
            evidence_id="ev-1",
            document_id="attendance-policy-v2",
            snippet="80% attendance is required to remain in good standing.",
        )
    ]
    result = extract_attendance_threshold(evidence)
    assert result.status == ThresholdExtractionStatus.OK
    assert result.required_percentage == Decimal("80")


def test_no_matching_section_is_not_found() -> None:
    evidence = [
        _evidence(
            evidence_id="ev-1",
            document_id="attendance-policy-v2",
            snippet="Condonation is available between 65% and 75% at the Dean's discretion.",
            section="Attendance Shortage Condonation",
        )
    ]
    result = extract_attendance_threshold(evidence)
    assert result.status == ThresholdExtractionStatus.NOT_FOUND


def test_empty_evidence_is_not_found() -> None:
    result = extract_attendance_threshold([])
    assert result.status == ThresholdExtractionStatus.NOT_FOUND
    assert result.required_percentage is None


def test_section_present_but_no_number_is_not_found() -> None:
    evidence = [
        _evidence(
            evidence_id="ev-1",
            document_id="attendance-policy-v2",
            snippet="Students must maintain the minimum attendance set by the Dean of Academics.",
        )
    ]
    result = extract_attendance_threshold(evidence)
    assert result.status == ThresholdExtractionStatus.NOT_FOUND


def test_ambiguous_threshold_needs_review() -> None:
    """Two active documents disagreeing on the numeric threshold -- must never guess."""
    evidence = [
        _evidence(
            evidence_id="ev-1",
            document_id="attendance-policy-v2",
            snippet="Students must maintain a minimum of 75% attendance in each enrolled course.",
            policy_version="v2",
        ),
        _evidence(
            evidence_id="ev-2",
            document_id="attendance-policy-v2-draft",
            snippet="Students must maintain a minimum of 80% attendance in each enrolled course.",
            policy_version="v2-draft",
        ),
    ]
    result = extract_attendance_threshold(evidence)
    assert result.status == ThresholdExtractionStatus.AMBIGUOUS
    assert result.required_percentage is None
    assert {c.required_percentage for c in result.candidates} == {Decimal("75"), Decimal("80")}


def test_repeated_identical_value_across_documents_is_not_ambiguous() -> None:
    evidence = [
        _evidence(
            evidence_id="ev-1",
            document_id="attendance-policy-v2",
            snippet="Students must maintain a minimum of 75% attendance in each enrolled course.",
        ),
        _evidence(
            evidence_id="ev-2",
            document_id="attendance-policy-v2",
            snippet="Students must maintain a minimum of 75% attendance in each enrolled course.",
        ),
    ]
    result = extract_attendance_threshold(evidence)
    assert result.status == ThresholdExtractionStatus.OK
    assert result.required_percentage == Decimal("75")
