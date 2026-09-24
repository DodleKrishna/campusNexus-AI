"""Deterministic candidate selectability (Phase 13).

Maps the named checks of ``check_event_registration`` -- the same rule the
Action Agent's pre-check and execute-time recheck use -- onto a candidate
status. No second copy of the registration rules exists: a candidate is
ELIGIBLE exactly when that shared rule passes with a performed schedule check.
Plain Python; the LLM never decides whether a candidate may be selected.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from app.schemas.selection import CandidateStatus
from app.schemas.verification import VerificationCheck

# First failing check wins, in this order. A missing student or event, or a
# registration the student already has, outranks the window/capacity checks.
_STATUS_BY_CHECK: List[Tuple[str, CandidateStatus]] = [
    ("student_exists", CandidateStatus.NEEDS_REVIEW),
    ("event_exists", CandidateStatus.UNAVAILABLE),
    ("not_already_registered", CandidateStatus.ALREADY_REGISTERED),
    ("registration_open", CandidateStatus.UNAVAILABLE),
    ("registration_deadline", CandidateStatus.DEADLINE_PASSED),
    ("capacity_available", CandidateStatus.FULL),
    ("no_schedule_conflicts", CandidateStatus.CONFLICT),
    ("schedule_conflict_checked", CandidateStatus.NEEDS_REVIEW),
]

_REASONS: Dict[CandidateStatus, str] = {
    CandidateStatus.NEEDS_REVIEW: "Could not be fully checked, so it cannot be selected.",
    CandidateStatus.UNAVAILABLE: "Registration is not open for this event.",
    CandidateStatus.ALREADY_REGISTERED: "You are already registered for this event.",
    CandidateStatus.DEADLINE_PASSED: "The registration deadline has passed.",
    CandidateStatus.FULL: "This event is full.",
    CandidateStatus.CONFLICT: "This event clashes with your schedule.",
}


_DETAILED = {CandidateStatus.UNAVAILABLE, CandidateStatus.NEEDS_REVIEW}


def derive_candidate_status(checks: Sequence[VerificationCheck]) -> Tuple[CandidateStatus, List[str]]:
    """``(status, reasons)`` for one candidate from its registration checks.

    ELIGIBLE requires every check to pass *and* the schedule check to have
    been performed (``no_schedule_conflicts`` only exists when it was).
    """
    by_name = {check.name: check for check in checks}
    for name, status in _STATUS_BY_CHECK:
        check = by_name.get(name)
        if check is not None and not check.passed:
            reasons = [_REASONS[status]]
            # The rule's own detail adds information only where the status alone is vague.
            if status in _DETAILED and check.detail:
                reasons.append(check.detail)
            return status, reasons
    if "no_schedule_conflicts" not in by_name or "event_exists" not in by_name:
        return CandidateStatus.NEEDS_REVIEW, [_REASONS[CandidateStatus.NEEDS_REVIEW]]
    return CandidateStatus.ELIGIBLE, []
