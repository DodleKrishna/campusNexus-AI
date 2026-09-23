"""Unit tests for deterministic SLA breach checking + window lookup (app/rules/sla.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.rules.sla import compute_sla_breaches, sla_windows_for_priority

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def test_no_breach_when_within_windows() -> None:
    response_breached, resolution_breached = compute_sla_breaches(
        response_due_at=NOW + timedelta(hours=1),
        resolution_due_at=NOW + timedelta(hours=10),
        responded_at=None,
        resolved_at=None,
        now=NOW,
    )
    assert response_breached is False
    assert resolution_breached is False


def test_response_breached_when_unresponded_past_due() -> None:
    response_breached, resolution_breached = compute_sla_breaches(
        response_due_at=NOW - timedelta(hours=1),
        resolution_due_at=NOW + timedelta(hours=10),
        responded_at=None,
        resolved_at=None,
        now=NOW,
    )
    assert response_breached is True
    assert resolution_breached is False


def test_resolution_breached_when_unresolved_past_due() -> None:
    response_breached, resolution_breached = compute_sla_breaches(
        response_due_at=NOW - timedelta(hours=10),
        resolution_due_at=NOW - timedelta(hours=1),
        responded_at=NOW - timedelta(hours=11),  # responded before its own due date
        resolved_at=None,
        now=NOW,
    )
    assert response_breached is False
    assert resolution_breached is True


def test_both_breached() -> None:
    response_breached, resolution_breached = compute_sla_breaches(
        response_due_at=NOW - timedelta(hours=10),
        resolution_due_at=NOW - timedelta(hours=1),
        responded_at=None,
        resolved_at=None,
        now=NOW,
    )
    assert response_breached is True
    assert resolution_breached is True


def test_responded_in_time_is_not_breached_even_if_now_is_later() -> None:
    response_breached, _ = compute_sla_breaches(
        response_due_at=NOW - timedelta(hours=1),
        resolution_due_at=NOW + timedelta(hours=10),
        responded_at=NOW - timedelta(hours=2),  # responded before the due date
        resolved_at=None,
        now=NOW,
    )
    assert response_breached is False


def test_responded_late_is_breached_even_though_it_happened() -> None:
    response_breached, _ = compute_sla_breaches(
        response_due_at=NOW - timedelta(hours=5),
        resolution_due_at=NOW + timedelta(hours=10),
        responded_at=NOW - timedelta(hours=1),  # responded, but after the due date
        resolved_at=None,
        now=NOW,
    )
    assert response_breached is True


def test_resolved_late_is_breached_even_though_the_case_is_closed() -> None:
    """A breach does not un-happen just because the case was eventually resolved."""
    _, resolution_breached = compute_sla_breaches(
        response_due_at=NOW - timedelta(hours=20),
        resolution_due_at=NOW - timedelta(hours=10),
        responded_at=NOW - timedelta(hours=19),
        resolved_at=NOW - timedelta(hours=1),  # resolved, but after the due date
        now=NOW,
    )
    assert resolution_breached is True


def test_exactly_at_due_date_is_not_breached() -> None:
    due = NOW
    response_breached, _ = compute_sla_breaches(
        response_due_at=due, resolution_due_at=NOW + timedelta(hours=1), responded_at=None, resolved_at=None, now=due
    )
    assert response_breached is False


# ---------------------------------------------------------------------------
# sla_windows_for_priority (Phase 7: source of the write tool's CaseSLA rows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "priority,expected",
    [("urgent", (2, 24)), ("high", (4, 24)), ("normal", (24, 120)), ("low", (48, 168))],
)
def test_sla_windows_match_the_grievance_sla_policy(priority: str, expected: tuple[int, int]) -> None:
    """Matches data/policies/grievance_sla_policy.md's "Response and Resolution
    SLA" section, and every scripts/seed_data.py CaseSeed row's own
    response_hours/resolution_hours for that priority."""
    assert sla_windows_for_priority(priority) == expected


def test_sla_windows_unknown_priority_raises() -> None:
    with pytest.raises(KeyError):
        sla_windows_for_priority("critical")
