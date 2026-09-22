"""Tests for the Phase 1 CampusNexus Pydantic contracts."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.approval import ApprovalRequest
from app.schemas.enums import (
    AgentName,
    AgentResultStatus,
    ApprovalStatus,
    MissionStatus,
    TaskStatus,
    ToolAccessType,
    ToolExecutionStatus,
    UserRole,
    VerificationPhase,
    VerificationStatus,
)
from app.schemas.evidence import Evidence
from app.schemas.mission import MissionPlan, MissionTask
from app.schemas.state import AuditEvent, CampusState, ErrorRecord
from app.schemas.tools import ToolCall, ToolResult
from app.schemas.verification import VerificationResult


# ---------------------------------------------------------------------------
# Valid model creation
# ---------------------------------------------------------------------------


def test_mission_task_valid_creation() -> None:
    task = MissionTask(
        task_id="task-1",
        mission_id="mission-1",
        agent=AgentName.ACADEMIC_AGENT,
        objective="Check attendance eligibility",
    )
    assert task.status == TaskStatus.PENDING
    assert task.dependencies == []
    assert task.created_at.tzinfo is not None


def test_agent_message_valid_creation() -> None:
    message = AgentMessage(
        message_id="msg-1",
        mission_id="mission-1",
        task_id="task-1",
        source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACADEMIC_AGENT,
        objective="Determine attendance eligibility",
        facts={"semester": 3},
    )
    assert message.facts["semester"] == 3
    assert message.evidence_refs == []


# ---------------------------------------------------------------------------
# Invalid blank IDs
# ---------------------------------------------------------------------------


def test_blank_mission_id_rejected() -> None:
    with pytest.raises(ValidationError):
        MissionTask(
            task_id="task-1",
            mission_id="   ",
            agent=AgentName.CAREER_AGENT,
            objective="Match internships",
        )


def test_blank_evidence_id_rejected() -> None:
    with pytest.raises(ValidationError):
        Evidence(
            evidence_id="",
            document_id="doc-1",
            title="Attendance Policy",
            source="handbook.pdf",
            snippet="Students must maintain 75% attendance.",
        )


# ---------------------------------------------------------------------------
# MissionTask self-dependency rejection
# ---------------------------------------------------------------------------


def test_mission_task_self_dependency_rejected() -> None:
    with pytest.raises(ValidationError):
        MissionTask(
            task_id="task-1",
            mission_id="mission-1",
            agent=AgentName.ACADEMIC_AGENT,
            objective="Check eligibility",
            dependencies=["task-1"],
        )


def test_mission_task_dependency_on_other_task_allowed() -> None:
    task = MissionTask(
        task_id="task-2",
        mission_id="mission-1",
        agent=AgentName.ACADEMIC_AGENT,
        objective="Check eligibility",
        dependencies=["task-1"],
    )
    assert task.dependencies == ["task-1"]


# ---------------------------------------------------------------------------
# ToolCall READ / WRITE + idempotency
# ---------------------------------------------------------------------------


def test_read_tool_call_without_idempotency_key_is_valid() -> None:
    call = ToolCall(
        tool_call_id="call-1",
        mission_id="mission-1",
        task_id="task-1",
        tool_name="get_attendance",
        read_or_write=ToolAccessType.READ,
    )
    assert call.idempotency_key is None
    assert call.requires_approval is False


def test_write_tool_call_without_idempotency_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolCall(
            tool_call_id="call-2",
            mission_id="mission-1",
            task_id="task-1",
            tool_name="file_hostel_ticket",
            read_or_write=ToolAccessType.WRITE,
        )


def test_write_tool_call_with_blank_idempotency_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolCall(
            tool_call_id="call-3",
            mission_id="mission-1",
            task_id="task-1",
            tool_name="file_hostel_ticket",
            read_or_write=ToolAccessType.WRITE,
            idempotency_key="   ",
        )


def test_valid_write_tool_call() -> None:
    call = ToolCall(
        tool_call_id="call-4",
        mission_id="mission-1",
        task_id="task-1",
        tool_name="file_hostel_ticket",
        arguments={"room": "B-204", "issue": "leaking tap"},
        read_or_write=ToolAccessType.WRITE,
        required_role=UserRole.STUDENT,
        requires_approval=True,
        idempotency_key="mission-1:task-1:file_hostel_ticket",
    )
    assert call.idempotency_key == "mission-1:task-1:file_hostel_ticket"
    assert call.requires_approval is True


# ---------------------------------------------------------------------------
# Evidence relevance score validation
# ---------------------------------------------------------------------------


def test_evidence_relevance_score_in_bounds() -> None:
    evidence = Evidence(
        evidence_id="ev-1",
        document_id="doc-1",
        title="Attendance Policy",
        source="handbook.pdf",
        snippet="Students must maintain 75% attendance.",
        relevance_score=0.87,
    )
    assert evidence.relevance_score == 0.87


@pytest.mark.parametrize("score", [-0.1, 1.1])
def test_evidence_relevance_score_out_of_bounds_rejected(score: float) -> None:
    with pytest.raises(ValidationError):
        Evidence(
            evidence_id="ev-1",
            document_id="doc-1",
            title="Attendance Policy",
            source="handbook.pdf",
            snippet="Students must maintain 75% attendance.",
            relevance_score=score,
        )


# ---------------------------------------------------------------------------
# ApprovalRequest
# ---------------------------------------------------------------------------


def test_approval_request_pending_creation() -> None:
    approval = ApprovalRequest(
        approval_id="appr-1",
        mission_id="mission-1",
        task_id="task-1",
        tool_call_id="call-4",
        action_summary="File a hostel maintenance ticket for room B-204",
        requested_by="student:12345",
    )
    assert approval.status == ApprovalStatus.PENDING
    assert approval.decision_by is None


def test_approval_request_resolved_requires_decision_fields() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(
            approval_id="appr-2",
            mission_id="mission-1",
            task_id="task-1",
            tool_call_id="call-4",
            action_summary="File a hostel maintenance ticket",
            requested_by="student:12345",
            status=ApprovalStatus.APPROVED,
        )


def test_approval_request_resolved_with_decision_fields_is_valid() -> None:
    approval = ApprovalRequest(
        approval_id="appr-3",
        mission_id="mission-1",
        task_id="task-1",
        tool_call_id="call-4",
        action_summary="File a hostel maintenance ticket",
        requested_by="student:12345",
        status=ApprovalStatus.APPROVED,
        decision_by="warden:staff-9",
        decision_at=datetime.now(timezone.utc),
        decision_reason="Valid maintenance request",
    )
    assert approval.status == ApprovalStatus.APPROVED


# ---------------------------------------------------------------------------
# VerificationResult
# ---------------------------------------------------------------------------


def test_verification_result_creation() -> None:
    result = VerificationResult(
        verification_id="ver-1",
        mission_id="mission-1",
        task_id="task-1",
        phase=VerificationPhase.PRE_ACTION,
        status=VerificationStatus.VERIFIED,
        checks=[{"name": "attendance_threshold", "passed": True}],
        evidence_refs=["ev-1"],
    )
    assert result.phase == VerificationPhase.PRE_ACTION
    assert result.checks[0].passed is True


# ---------------------------------------------------------------------------
# CampusState partial lifecycle construction
# ---------------------------------------------------------------------------


def test_campus_state_partial_lifecycle_construction() -> None:
    state = CampusState(
        mission_id="mission-1",
        user_id="student-42",
        user_role=UserRole.STUDENT,
        original_goal="Help me resolve my hostel complaint",
    )
    assert state.mission_status == MissionStatus.PENDING
    assert state.plan is None
    assert state.agent_results == []
    assert state.pending_approvals == []
    assert state.final_result is None


def test_campus_state_with_plan_and_results() -> None:
    plan = MissionPlan(
        mission_id="mission-1",
        goal="Resolve hostel complaint",
        tasks=[
            MissionTask(
                task_id="task-1",
                mission_id="mission-1",
                agent=AgentName.CAMPUS_SERVICES_AGENT,
                objective="Gather complaint details",
            )
        ],
    )
    result = AgentResult(
        mission_id="mission-1",
        task_id="task-1",
        agent=AgentName.CAMPUS_SERVICES_AGENT,
        status=AgentResultStatus.SUCCESS,
    )
    state = CampusState(
        mission_id="mission-1",
        user_id="student-42",
        user_role=UserRole.STUDENT,
        original_goal="Help me resolve my hostel complaint",
        mission_status=MissionStatus.IN_PROGRESS,
        plan=plan,
        agent_results=[result],
    )
    assert state.plan.tasks[0].task_id == "task-1"
    assert state.agent_results[0].status == AgentResultStatus.SUCCESS


def test_mission_plan_rejects_task_from_other_mission() -> None:
    with pytest.raises(ValidationError):
        MissionPlan(
            mission_id="mission-1",
            goal="Resolve hostel complaint",
            tasks=[
                MissionTask(
                    task_id="task-1",
                    mission_id="mission-OTHER",
                    agent=AgentName.CAMPUS_SERVICES_AGENT,
                    objective="Gather complaint details",
                )
            ],
        )


# ---------------------------------------------------------------------------
# AuditEvent / ErrorRecord
# ---------------------------------------------------------------------------


def test_audit_event_and_error_record_creation() -> None:
    audit = AuditEvent(
        event_id="audit-1",
        mission_id="mission-1",
        task_id="task-1",
        event_type="approval_granted",
        actor="warden:staff-9",
        message="Approved hostel maintenance ticket filing",
    )
    error = ErrorRecord(
        error_id="err-1",
        mission_id="mission-1",
        component="action_agent",
        error_type="ToolTimeout",
        message="file_hostel_ticket timed out",
        retryable=True,
    )
    assert audit.task_id == "task-1"
    assert error.task_id is None
    assert error.retryable is True


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------


def test_tool_result_creation() -> None:
    result = ToolResult(
        tool_call_id="call-4",
        status=ToolExecutionStatus.SUCCESS,
        data={"ticket_id": "TCK-99"},
        postcondition_verified=True,
    )
    assert result.status == ToolExecutionStatus.SUCCESS
    assert result.postcondition_verified is True


# ---------------------------------------------------------------------------
# JSON serialization / deserialization round trip
# ---------------------------------------------------------------------------


def test_campus_state_json_round_trip() -> None:
    plan = MissionPlan(
        mission_id="mission-1",
        goal="Resolve hostel complaint",
        tasks=[
            MissionTask(
                task_id="task-1",
                mission_id="mission-1",
                agent=AgentName.CAMPUS_SERVICES_AGENT,
                objective="Gather complaint details",
                dependencies=[],
            )
        ],
    )
    state = CampusState(
        mission_id="mission-1",
        user_id="student-42",
        user_role=UserRole.STUDENT,
        original_goal="Help me resolve my hostel complaint",
        mission_status=MissionStatus.IN_PROGRESS,
        plan=plan,
    )

    payload = state.model_dump_json()
    restored = CampusState.model_validate_json(payload)

    assert restored == state
    assert restored.plan.tasks[0].agent == AgentName.CAMPUS_SERVICES_AGENT


def test_tool_call_json_round_trip() -> None:
    call = ToolCall(
        tool_call_id="call-4",
        mission_id="mission-1",
        task_id="task-1",
        tool_name="file_hostel_ticket",
        arguments={"room": "B-204", "issue": "leaking tap"},
        read_or_write=ToolAccessType.WRITE,
        idempotency_key="mission-1:task-1:file_hostel_ticket",
    )
    restored = ToolCall.model_validate_json(call.model_dump_json())
    assert restored == call
