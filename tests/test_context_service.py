"""Tests for the deterministic Context Service (app/services/context.py)."""
from __future__ import annotations

import pytest

from app.db.base import utc_now
from app.schemas.enums import (
    AgentName,
    AgentResultStatus,
    ApprovalStatus,
    MissionStatus,
    TaskStatus,
    ToolAccessType,
    ToolExecutionStatus,
    UserRole,
)
from app.services.context import ContextService
from scripts.seed_data import DEMO_STUDENT_CODE


@pytest.fixture()
def ctx(seeded_session) -> ContextService:
    return ContextService(seeded_session)


def _make_mission(ctx: ContextService, mission_id: str = "mission-1") -> None:
    ctx.create_mission(
        mission_id=mission_id,
        user_id=DEMO_STUDENT_CODE,
        user_role=UserRole.STUDENT,
        original_goal="Help me plan my semester",
    )


# ---------------------------------------------------------------------------
# Mission lifecycle
# ---------------------------------------------------------------------------


def test_create_and_get_mission(ctx: ContextService) -> None:
    _make_mission(ctx)
    mission = ctx.get_mission("mission-1")
    assert mission is not None
    assert mission.status == MissionStatus.PENDING
    assert mission.user_id == DEMO_STUDENT_CODE


def test_get_mission_returns_none_for_unknown_id(ctx: ContextService) -> None:
    assert ctx.get_mission("does-not-exist") is None


def test_update_mission_status(ctx: ContextService) -> None:
    _make_mission(ctx)
    updated = ctx.update_mission_status("mission-1", MissionStatus.IN_PROGRESS)
    assert updated.status == MissionStatus.IN_PROGRESS

    completed = ctx.update_mission_status(
        "mission-1", MissionStatus.COMPLETED, final_result="Semester plan generated"
    )
    assert completed.status == MissionStatus.COMPLETED
    assert completed.final_result == "Semester plan generated"


def test_update_mission_status_unknown_mission_raises(ctx: ContextService) -> None:
    with pytest.raises(ValueError):
        ctx.update_mission_status("nope", MissionStatus.IN_PROGRESS)


# ---------------------------------------------------------------------------
# Mission steps
# ---------------------------------------------------------------------------


def test_create_and_update_mission_step(ctx: ContextService) -> None:
    _make_mission(ctx)
    step = ctx.create_mission_step(
        step_id="step-1", mission_id="mission-1", agent=AgentName.ACADEMIC_AGENT,
        objective="Check attendance eligibility", sequence=1,
    )
    assert step.status == TaskStatus.PENDING

    started = ctx.update_mission_step("step-1", status=TaskStatus.IN_PROGRESS, started_at=utc_now())
    assert started.status == TaskStatus.IN_PROGRESS
    assert started.started_at is not None

    completed = ctx.update_mission_step("step-1", status=TaskStatus.COMPLETED, completed_at=utc_now())
    assert completed.status == TaskStatus.COMPLETED


def test_create_mission_step_requires_existing_mission(ctx: ContextService) -> None:
    with pytest.raises(Exception):
        ctx.create_mission_step(
            step_id="step-orphan", mission_id="no-such-mission", agent=AgentName.ACADEMIC_AGENT,
            objective="x",
        )


# ---------------------------------------------------------------------------
# Agent results / tool calls
# ---------------------------------------------------------------------------


def test_record_agent_result(ctx: ContextService) -> None:
    _make_mission(ctx)
    ctx.create_mission_step(
        step_id="step-1", mission_id="mission-1", agent=AgentName.ACADEMIC_AGENT, objective="Check attendance"
    )
    run = ctx.record_agent_result(
        mission_id="mission-1", step_id="step-1", agent=AgentName.ACADEMIC_AGENT,
        status=AgentResultStatus.SUCCESS, facts={"attendance_percent_source": "raw_counts"},
    )
    assert run.status == AgentResultStatus.SUCCESS
    assert run.facts["attendance_percent_source"] == "raw_counts"


def test_record_tool_call_deduplicates_on_idempotency_key(ctx: ContextService) -> None:
    _make_mission(ctx)
    ctx.create_mission_step(
        step_id="step-1", mission_id="mission-1", agent=AgentName.ACTION_AGENT, objective="File a ticket"
    )
    first = ctx.record_tool_call(
        tool_call_id="call-1", mission_id="mission-1", step_id="step-1", tool_name="file_hostel_ticket",
        read_or_write=ToolAccessType.WRITE, idempotency_key="mission-1:step-1:file_hostel_ticket",
        status=ToolExecutionStatus.SUCCESS,
    )
    second = ctx.record_tool_call(
        tool_call_id="call-2", mission_id="mission-1", step_id="step-1", tool_name="file_hostel_ticket",
        read_or_write=ToolAccessType.WRITE, idempotency_key="mission-1:step-1:file_hostel_ticket",
        status=ToolExecutionStatus.SUCCESS,
    )
    assert first.tool_call_id == second.tool_call_id == "call-1"


def test_record_tool_call_without_idempotency_key_never_dedupes(ctx: ContextService) -> None:
    _make_mission(ctx)
    ctx.create_mission_step(
        step_id="step-1", mission_id="mission-1", agent=AgentName.ACTION_AGENT, objective="Read data"
    )
    first = ctx.record_tool_call(
        tool_call_id="call-1", mission_id="mission-1", step_id="step-1", tool_name="get_attendance",
        read_or_write=ToolAccessType.READ,
    )
    second = ctx.record_tool_call(
        tool_call_id="call-2", mission_id="mission-1", step_id="step-1", tool_name="get_attendance",
        read_or_write=ToolAccessType.READ,
    )
    assert first.tool_call_id != second.tool_call_id


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


def test_create_and_approve_approval_record(ctx: ContextService) -> None:
    _make_mission(ctx)
    ctx.create_mission_step(
        step_id="step-1", mission_id="mission-1", agent=AgentName.ACTION_AGENT, objective="File a ticket"
    )
    approval = ctx.create_approval_record(
        approval_id="appr-1", mission_id="mission-1", step_id="step-1",
        action_summary="File hostel maintenance ticket", requested_by=DEMO_STUDENT_CODE,
    )
    assert approval.status == ApprovalStatus.PENDING

    resolved = ctx.update_approval_record(
        "appr-1", ApprovalStatus.APPROVED, decision_by="warden:staff-9", decision_at=utc_now(),
    )
    assert resolved.status == ApprovalStatus.APPROVED
    assert resolved.decision_by == "warden:staff-9"


def test_update_approval_record_cannot_resolve_without_decision_fields(ctx: ContextService) -> None:
    _make_mission(ctx)
    ctx.create_mission_step(
        step_id="step-1", mission_id="mission-1", agent=AgentName.ACTION_AGENT, objective="File a ticket"
    )
    ctx.create_approval_record(
        approval_id="appr-1", mission_id="mission-1", step_id="step-1",
        action_summary="File hostel maintenance ticket", requested_by=DEMO_STUDENT_CODE,
    )
    with pytest.raises(ValueError):
        ctx.update_approval_record("appr-1", ApprovalStatus.APPROVED)


def test_update_approval_record_unknown_id_raises(ctx: ContextService) -> None:
    with pytest.raises(ValueError):
        ctx.update_approval_record("nope", ApprovalStatus.REJECTED, decision_by="x", decision_at=utc_now())


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def test_append_audit_event(ctx: ContextService) -> None:
    _make_mission(ctx)
    event = ctx.append_audit_event(
        event_id="audit-1", mission_id="mission-1", event_type="mission_created",
        actor=DEMO_STUDENT_CODE, message="Mission created",
    )
    assert event.event_type == "mission_created"


def test_audit_trail_is_append_only_and_ordered(ctx: ContextService) -> None:
    _make_mission(ctx)
    ctx.append_audit_event(
        event_id="audit-1", mission_id="mission-1", event_type="mission_created",
        actor=DEMO_STUDENT_CODE, message="first",
    )
    ctx.append_audit_event(
        event_id="audit-2", mission_id="mission-1", event_type="step_started",
        actor=DEMO_STUDENT_CODE, message="second",
    )
    from app.db.repositories.missions import get_audit_trail

    trail = get_audit_trail(ctx._session, "mission-1")
    assert [e.event_id for e in trail] == ["audit-1", "audit-2"]


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def test_store_and_get_relevant_memories(ctx: ContextService) -> None:
    ctx.store_memory(
        memory_id="mem-1", category="preference", content="Prefers email over SMS notifications.",
        student_id=DEMO_STUDENT_CODE,
    )
    ctx.store_memory(
        memory_id="mem-2", category="fact", content="Completed OS course prerequisite.",
        student_id=DEMO_STUDENT_CODE,
    )
    memories = ctx.get_relevant_memories(student_id=DEMO_STUDENT_CODE)
    assert {m.memory_id for m in memories} == {"mem-1", "mem-2"}

    preferences_only = ctx.get_relevant_memories(student_id=DEMO_STUDENT_CODE, category="preference")
    assert [m.memory_id for m in preferences_only] == ["mem-1"]


# ---------------------------------------------------------------------------
# Student context
# ---------------------------------------------------------------------------


def test_get_student_context_returns_compact_snapshot(ctx: ContextService) -> None:
    context = ctx.get_student_context(DEMO_STUDENT_CODE)
    assert context.student_code == DEMO_STUDENT_CODE
    assert context.department_code == "CSE"
    assert context.cgpa == 7.8
    assert "CS301" in context.course_codes
    os_attendance = next(a for a in context.attendance if a["course_code"] == "CS301")
    assert os_attendance["classes_attended"] == 34
    assert os_attendance["classes_conducted"] == 50
    assert "CASE-0001" in context.open_case_codes
    assert context.application_count >= 1


def test_get_student_context_unknown_student_raises(ctx: ContextService) -> None:
    with pytest.raises(ValueError):
        ctx.get_student_context("STU-NOT-REAL")
