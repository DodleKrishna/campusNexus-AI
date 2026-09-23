"""Unit tests for ActionVerifier's check-aggregation logic
(app/verification/action.py) -- the first real use of
VerificationPhase.PRE_ACTION/POST_ACTION.
"""
from __future__ import annotations

from app.schemas.enums import VerificationPhase, VerificationStatus
from app.schemas.verification import VerificationCheck
from app.verification.action import ActionVerifier


def test_all_passed_is_verified() -> None:
    verifier = ActionVerifier()
    result = verifier.verify_pre_action(
        mission_id="m1", task_id="t1",
        checks=[VerificationCheck(name="student_exists", passed=True), VerificationCheck(name="event_exists", passed=True)],
    )
    assert result.status == VerificationStatus.VERIFIED
    assert result.phase == VerificationPhase.PRE_ACTION
    assert result.issues == []


def test_hard_failure_is_failed() -> None:
    verifier = ActionVerifier()
    result = verifier.verify_pre_action(
        mission_id="m1", task_id="t1",
        checks=[VerificationCheck(name="student_exists", passed=False, detail="no student")],
    )
    assert result.status == VerificationStatus.FAILED
    assert "no student" in result.issues


def test_soft_failure_downgrades_to_needs_review_not_failed() -> None:
    verifier = ActionVerifier()
    result = verifier.verify_pre_action(
        mission_id="m1", task_id="t1",
        checks=[
            VerificationCheck(name="student_exists", passed=True),
            VerificationCheck(name="no_schedule_conflicts", passed=False, detail="1 conflict"),
        ],
    )
    assert result.status == VerificationStatus.NEEDS_REVIEW


def test_hard_failure_takes_precedence_over_soft_failure() -> None:
    verifier = ActionVerifier()
    result = verifier.verify_pre_action(
        mission_id="m1", task_id="t1",
        checks=[
            VerificationCheck(name="capacity_available", passed=False, detail="full"),
            VerificationCheck(name="no_schedule_conflicts", passed=False, detail="1 conflict"),
        ],
    )
    assert result.status == VerificationStatus.FAILED


def test_post_action_phase_is_set() -> None:
    verifier = ActionVerifier()
    result = verifier.verify_post_action(
        mission_id="m1", task_id="t1", checks=[VerificationCheck(name="registration_persisted", passed=True)]
    )
    assert result.phase == VerificationPhase.POST_ACTION
    assert result.status == VerificationStatus.VERIFIED


def test_post_action_failure_never_marked_verified() -> None:
    verifier = ActionVerifier()
    result = verifier.verify_post_action(
        mission_id="m1", task_id="t1",
        checks=[VerificationCheck(name="registration_persisted", passed=False, detail="row not found")],
    )
    assert result.status == VerificationStatus.FAILED


def test_evidence_refs_are_carried_through() -> None:
    from app.schemas.evidence import Evidence

    verifier = ActionVerifier()
    ev = Evidence(evidence_id="ev-1", document_id="doc-1", title="t", source="s", snippet="text")
    result = verifier.verify_pre_action(
        mission_id="m1", task_id="t1", checks=[VerificationCheck(name="student_exists", passed=True)], evidence=[ev]
    )
    assert result.evidence_refs == ["ev-1"]
