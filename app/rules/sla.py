"""Deterministic SLA breach checking + window lookup.

``CaseSLA.response_due_at``/``resolution_due_at`` are pre-computed absolute
timestamps -- at seed time from the Grievance SLA Policy's numeric windows,
and (Phase 7) at ``create_campus_case`` write-tool time via
``sla_windows_for_priority`` below -- the same "the DB already holds the
authoritative number" situation as opportunity eligibility's
``eligible_years``, so no policy-prose parsing is needed at breach-check
time; this module is purely the breach comparison plus the one deterministic
lookup table the policy actually defines. A breach is a missed deadline and
does not "un-happen" if the case is later responded to/resolved late --
``responded``/``resolved`` are checked against their due date when they
happened, or against ``now`` when they haven't happened yet.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

# Source: data/policies/grievance_sla_policy.md, "Response and Resolution
# SLA" -- (response_hours, resolution_hours) per priority. Verified against
# every CaseSeed row in scripts/seed_data.py (each seeded case's own
# response_hours/resolution_hours matches its priority's row here exactly).
SLA_WINDOWS_HOURS = {
    "urgent": (2, 24),
    "high": (4, 24),
    "normal": (24, 120),
    "low": (48, 168),
}


def sla_windows_for_priority(priority: str) -> Tuple[int, int]:
    """Returns ``(response_hours, resolution_hours)`` for a case priority.

    Raises ``KeyError`` for an unrecognized priority -- callers (the
    ``create_campus_case`` write tool) only ever reach this after the
    deterministic ``valid_priority`` precondition check has already passed,
    so an unrecognized value here would be a programming error, not a
    reachable runtime state worth swallowing silently.
    """
    return SLA_WINDOWS_HOURS[priority]


def _is_breached(actual_at: Optional[datetime], due_at: datetime, *, now: datetime) -> bool:
    if actual_at is None:
        return now > due_at
    return actual_at > due_at


def compute_sla_breaches(
    *,
    response_due_at: datetime,
    resolution_due_at: datetime,
    responded_at: Optional[datetime],
    resolved_at: Optional[datetime],
    now: datetime,
) -> Tuple[bool, bool]:
    """Returns ``(response_breached, resolution_breached)``."""
    response_breached = _is_breached(responded_at, response_due_at, now=now)
    resolution_breached = _is_breached(resolved_at, resolution_due_at, now=now)
    return response_breached, resolution_breached
