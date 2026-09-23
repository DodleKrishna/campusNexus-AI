"""Tests for the Approval Gate (app/services/approval_gate.py):
CLAUDE.md's Permission/Approval Rules -- no silent auto-approve, a resolved
approval can never be silently re-decided, a rejected action's step is
flipped to FAILED (never re-dispatched), an approved action's step is
flipped to PENDING (re-dispatchable).
"""
from __future__ import annotations

import pytest

from app.schemas.enums import AgentName, ApprovalStatus, TaskStatus, UserRole
from app.services.approval_gate import ApprovalGate, ApprovalGateError
from app.services.context import ContextService

MISSION_USER = "STU-DEMO-001"


def _setup_step(session, mission_id="m-1", step_id="t-1") -> ContextService:
    context = ContextService(session)
    context.create_mission(mission_id, MISSION_USER, UserRole.STUDENT, "test goal")
    context.create_mission_step(step_id, mission_id, AgentName.ACTION_AGENT, "do the thing")
    return context


def test_request_approval_creates_pending_record_and_audit_event(session) -> None:
    context = _setup_step(session)
    gate = ApprovalGate(session)
    record = gate.request_approval(
        mission_id="m-1", step_id="t-1", tool_call_id=None, action_summary="Register for X.", requested_by="mission_orchestrator"
    )
    assert record.status == ApprovalStatus.PENDING
    event_types = [e.event_type for e in context.list_audit_events("m-1")]
    assert "approval_requested" in event_types


def test_decide_approved_flips_step_to_pending(session) -> None:
    context = _setup_step(session)
    context.update_mission_step("t-1", status=TaskStatus.BLOCKED)
    gate = ApprovalGate(session)
    record = gate.request_approval(mission_id="m-1", step_id="t-1", tool_call_id=None, action_summary="x", requested_by="r")

    gate.decide(record.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    step = context.get_mission("m-1").steps[0]
    assert step.status == TaskStatus.PENDING
    event_types = [e.event_type for e in context.list_audit_events("m-1")]
    assert "approval_approved" in event_types


def test_decide_rejected_flips_step_to_failed(session) -> None:
    context = _setup_step(session)
    context.update_mission_step("t-1", status=TaskStatus.BLOCKED)
    gate = ApprovalGate(session)
    record = gate.request_approval(mission_id="m-1", step_id="t-1", tool_call_id=None, action_summary="x", requested_by="r")

    gate.decide(record.approval_id, decision=ApprovalStatus.REJECTED, decision_by="admin-demo", decision_reason="not needed")

    step = context.get_mission("m-1").steps[0]
    assert step.status == TaskStatus.FAILED
    assert step.completed_at is not None


def test_decide_on_unknown_approval_raises(session) -> None:
    gate = ApprovalGate(session)
    with pytest.raises(ApprovalGateError, match="unknown approval_id"):
        gate.decide("appr-nope", decision=ApprovalStatus.APPROVED, decision_by="admin")


def test_decide_already_resolved_approval_raises_never_silently_reapplied(session) -> None:
    _setup_step(session)
    gate = ApprovalGate(session)
    record = gate.request_approval(mission_id="m-1", step_id="t-1", tool_call_id=None, action_summary="x", requested_by="r")
    gate.decide(record.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    with pytest.raises(ApprovalGateError, match="already resolved"):
        gate.decide(record.approval_id, decision=ApprovalStatus.REJECTED, decision_by="someone-else")


def test_decide_only_accepts_approved_or_rejected(session) -> None:
    _setup_step(session)
    gate = ApprovalGate(session)
    record = gate.request_approval(mission_id="m-1", step_id="t-1", tool_call_id=None, action_summary="x", requested_by="r")
    with pytest.raises(ApprovalGateError, match="only accepts APPROVED/REJECTED"):
        gate.decide(record.approval_id, decision=ApprovalStatus.PENDING, decision_by="admin")


def test_mark_superseded_transitions_to_edit_required(session) -> None:
    context = _setup_step(session)
    gate = ApprovalGate(session)
    record = gate.request_approval(mission_id="m-1", step_id="t-1", tool_call_id=None, action_summary="x", requested_by="r")

    updated = gate.mark_superseded(record.approval_id, requested_by="admin-demo", reason="wrong category")

    assert updated.status == ApprovalStatus.EDIT_REQUIRED
    event_types = [e.event_type for e in context.list_audit_events("m-1")]
    assert "approval_edit_requested" in event_types


def test_mark_superseded_on_rejected_approval_raises(session) -> None:
    _setup_step(session)
    gate = ApprovalGate(session)
    record = gate.request_approval(mission_id="m-1", step_id="t-1", tool_call_id=None, action_summary="x", requested_by="r")
    gate.decide(record.approval_id, decision=ApprovalStatus.REJECTED, decision_by="admin-demo")

    with pytest.raises(ApprovalGateError, match="cannot be edited"):
        gate.mark_superseded(record.approval_id, requested_by="admin-demo")
