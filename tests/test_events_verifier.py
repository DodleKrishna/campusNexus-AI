"""Tests for the deterministic EventsVerifier (app/verification/events.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas.enums import VerificationStatus
from app.schemas.events import EventAssessment, EventAvailabilityStatus, EventsIntent, EventSummary
from app.schemas.evidence import Evidence
from app.verification.events import EventsVerificationInput, EventsVerifier

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)

EVENT = EventSummary(
    event_id=1, title="AI Workshop", description="An AI workshop.", category="workshop", organizer="AI/ML Club",
    location="Hall 1", start_at=NOW + timedelta(days=5), end_at=NOW + timedelta(days=5, hours=1), status="open",
)

ASSESSMENT_NO_CHECK = EventAssessment(event=EVENT, availability=EventAvailabilityStatus.AVAILABLE, conflict_check_performed=False)
ASSESSMENT_CHECKED = EventAssessment(event=EVENT, availability=EventAvailabilityStatus.AVAILABLE, conflict_check_performed=True)

EVIDENCE = [Evidence(evidence_id="ev-1", document_id="event-policy", title="Event Policy", source="x", snippet="s")]

verifier = EventsVerifier()


def _base_input(**overrides) -> EventsVerificationInput:
    defaults = dict(
        mission_id="m1", task_id="t1", intent=EventsIntent.EVENT_DISCOVERY, student_exists=True,
        requires_events=True, requires_policy_evidence=True, expected_conflict_check=False,
        assessments=[ASSESSMENT_NO_CHECK], evidence=EVIDENCE,
    )
    defaults.update(overrides)
    return EventsVerificationInput(**defaults)


def test_fully_verified_case() -> None:
    result = verifier.verify(_base_input())
    assert result.status == VerificationStatus.VERIFIED


def test_missing_student_is_failed() -> None:
    result = verifier.verify(_base_input(student_exists=False))
    assert result.status == VerificationStatus.FAILED


def test_unsupported_intent_is_failed() -> None:
    result = verifier.verify(
        _base_input(intent=EventsIntent.UNKNOWN, requires_events=False, requires_policy_evidence=False, assessments=[], evidence=[])
    )
    assert result.status == VerificationStatus.FAILED


def test_no_events_found_is_needs_review() -> None:
    result = verifier.verify(_base_input(assessments=[]))
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_expected_conflict_check_not_performed_is_needs_review() -> None:
    result = verifier.verify(_base_input(expected_conflict_check=True, assessments=[ASSESSMENT_NO_CHECK]))
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_expected_conflict_check_performed_is_verified() -> None:
    result = verifier.verify(_base_input(expected_conflict_check=True, assessments=[ASSESSMENT_CHECKED]))
    assert result.status == VerificationStatus.VERIFIED


def test_missing_evidence_is_needs_review() -> None:
    result = verifier.verify(_base_input(evidence=[]))
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_registration_status_intent_does_not_require_conflict_check() -> None:
    result = verifier.verify(
        _base_input(intent=EventsIntent.REGISTRATION_STATUS, requires_policy_evidence=False, evidence=[])
    )
    assert result.status == VerificationStatus.VERIFIED
