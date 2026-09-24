"""Deterministic mapping from real persisted execution events to the mission
timeline's status vocabulary (Phase 8 §7): PLANNING/RUNNING/VERIFYING/
WAITING_FOR_APPROVAL/COMPLETED/NEEDS_REVIEW/FAILED.

Pure function over already-fetched ORM rows -- no I/O, no fabrication. Two
kinds of *real* persisted events are merged and sorted chronologically:

1. Each ``MissionStep.started_at`` (already persisted by the Orchestrator
   before every dispatch) becomes a RUNNING entry -- this is what lets the
   timeline show a task actually starting, without inventing a timestamp
   nothing in the DB ever recorded.
2. Every ``AuditLog`` row, whose ``event_type`` (and, for ``task_verified``,
   the verification status embedded in its own message text) maps
   deterministically to the vocabulary above.
"""
from __future__ import annotations

from typing import List

from app.api.schemas.missions import TimelineEntryView

_VERIFICATION_TEXT_TO_STATUS = {
    "verified": "COMPLETED",
    "needs_review": "NEEDS_REVIEW",
    "failed": "FAILED",
}
_MISSION_STATUS_TEXT_TO_STATUS = {
    "completed": "COMPLETED",
    "failed": "FAILED",
    "needs_approval": "WAITING_FOR_APPROVAL",
    "needs_replan": "RUNNING",
    "cancelled": "FAILED",
    "pending": "PLANNING",
    "planning": "PLANNING",
    "in_progress": "RUNNING",
}

_EVENT_TYPE_TO_STATUS = {
    "mission_created": "PLANNING",
    "mission_resumed": "RUNNING",
    "plan_generated": "PLANNING",
    "plan_invalid": "FAILED",
    "task_failed": "FAILED",
    "task_skipped": "FAILED",
    "replan_triggered": "RUNNING",
    "duplicate_failure_detected": "FAILED",
    "approval_requested": "WAITING_FOR_APPROVAL",
    "approval_approved": "RUNNING",
    "approval_rejected": "FAILED",
    "approval_edit_requested": "WAITING_FOR_APPROVAL",
    # Phase 11: not a rejection -- the approval expired because conditions changed.
    "approval_invalidated": "NEEDS_REVIEW",
    # Phase 12B: not a failure -- the student has to pick the target themselves.
    "action_target_unconfirmed": "NEEDS_REVIEW",
}


def _status_for_audit_event(event) -> str:
    if event.event_type == "task_verified":
        # Message format (app/graph/orchestrator.py): "Task {id} verification: {status}".
        suffix = event.message.rsplit(": ", 1)[-1].strip().lower()
        return _VERIFICATION_TEXT_TO_STATUS.get(suffix, "NEEDS_REVIEW")
    if event.event_type == "mission_finalized":
        suffix = event.message.rsplit(" ", 1)[-1].strip(".").lower()
        return _MISSION_STATUS_TEXT_TO_STATUS.get(suffix, "COMPLETED")
    return _EVENT_TYPE_TO_STATUS.get(event.event_type, "RUNNING")


def build_timeline(steps, audit_events) -> List[TimelineEntryView]:
    """``steps``: MissionStep rows. ``audit_events``: AuditLog rows (any order)."""
    entries: List[TimelineEntryView] = []

    for step in steps:
        if step.started_at is not None:
            entries.append(
                TimelineEntryView(
                    event_id=f"step-started-{step.step_id}",
                    timestamp=step.started_at,
                    status="RUNNING",
                    event_type="task_started",
                    actor="mission_orchestrator",
                    message=f"Task {step.step_id} ({step.agent.value}) started.",
                    step_id=step.step_id,
                )
            )

    for event in audit_events:
        entries.append(
            TimelineEntryView(
                event_id=event.event_id,
                timestamp=event.timestamp,
                status=_status_for_audit_event(event),
                event_type=event.event_type,
                actor=event.actor,
                message=event.message,
                step_id=event.step_id,
            )
        )

    entries.sort(key=lambda e: e.timestamp)
    return entries
