"""Phase 11 -- approval lifecycle hardening.

A human approval authorizes one exact, validated action under the conditions
that held when it was granted. These tests prove that:

A. a valid approval still executes, exactly once;
B-D. a schedule change / capacity change / passed deadline after approval
     blocks the write *and* marks the approval STALE (zero rows written);
E. a changed payload can never ride on the old approval;
F. a stale approval stays unusable after a process restart;
G-H. when the world becomes valid again a NEW approval is required, and
     approving it produces exactly one write;
I. the audit trail records who approved, when, why it went stale and what
   changed;
J. resuming repeatedly with a stale approval never re-attempts execution.

Plus the fingerprint module, the additive schema upgrade, and the API/UI
surface. Offline: isolated per-test DB/Chroma, mock LLM, no dev DB.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.api.timeline import build_timeline
from app.db.base import utc_now
from app.db.models.academic import Exam
from app.db.models.events import Event, EventRegistration, RegistrationStatus
from app.db.models.mission import ApprovalRecord, ToolCallRecord
from app.db.repositories.missions import get_latest_approval_for_step, list_approvals_for_step
from app.db.session import create_db_engine, create_session_factory, upgrade_schema
from app.schemas.action import RegisterEventInput
from app.schemas.enums import AgentResultStatus, ApprovalStatus, MissionStatus, ToolExecutionStatus, VerificationStatus
from app.services.approval_binding import (
    FINGERPRINT_FIELDS,
    approval_fingerprint,
    build_approval_payload,
    normalize_arguments,
)
from app.services.approval_gate import ApprovalGate, ApprovalGateError
from app.services.context import ContextService
from tests.test_action_safety import (
    CLEAN_EVENT_ID,
    DEMO_STUDENT,
    _agent,
    _message,
    _propose_and_approve,
    _registration_count,
)
from tests.test_phase10_mission_flow import (
    REGISTER_GOAL,
    _action_task,
    _add_exam_over_event,
    _approve,
    _production_orchestrator,
    _registration_rows,
    _run,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RESCHEDULED_LOCATION = "Rescheduled Exam Hall"  # what _add_exam_over_event writes
# What the Orchestrator re-sends on every dispatch of the action step (its plan constraints).
REGISTER_CONSTRAINTS = {"tool_name": "register_event", "event_title": "Competitive Coding Contest"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _approval(session, task_id: str) -> ApprovalRecord:
    session.expire_all()
    return get_latest_approval_for_step(session, task_id)


def _step_approvals(session, task_id: str):
    session.expire_all()
    return list_approvals_for_step(session, task_id)


def _add_exam_over_clean_event(session) -> None:
    from app.db.models.academic import Enrollment
    from app.db.models.identity import Student

    student = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    enrollment = session.execute(select(Enrollment).where(Enrollment.student_id == student.id)).scalars().first()
    event = session.get(Event, CLEAN_EVENT_ID)
    session.add(
        Exam(organization_id=student.organization_id, course_id=enrollment.course_id, exam_type="quiz",
             scheduled_start=event.start_at, scheduled_end=event.end_at, location=RESCHEDULED_LOCATION)
    )
    session.commit()


def _remove_rescheduled_exams(session) -> None:
    for exam in session.execute(select(Exam).where(Exam.location == RESCHEDULED_LOCATION)).scalars().all():
        session.delete(exam)
    session.commit()


def _audit_events(session, mission_id: str, event_type: str):
    return [e for e in ContextService(session).list_audit_events(mission_id) if e.event_type == event_type]


def _successful_tool_calls(session, mission_id: str) -> int:
    session.expire_all()
    rows = session.execute(
        select(ToolCallRecord).where(
            ToolCallRecord.mission_id == mission_id, ToolCallRecord.status == ToolExecutionStatus.SUCCESS
        )
    ).scalars().all()
    return len(rows)


def _go_stale_on_schedule_change(seeded_session, knowledge_service, mission_id: str, task_id: str = "t-1"):
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)
    approved = _approval(seeded_session, task_id)
    _add_exam_over_clean_event(seeded_session)
    outcome = _agent(seeded_session, knowledge_service).handle(_message(mission_id, task_id))
    return approved.approval_id, outcome


# ---------------------------------------------------------------------------
# fingerprint (pure)
# ---------------------------------------------------------------------------


def _payload(**overrides):
    base = dict(mission_id="m-1", step_id="t-1", tool_name="register_event",
                arguments={"student_id": DEMO_STUDENT, "event_id": 10}, precheck_status="verified")
    base.update(overrides)
    return build_approval_payload(**base)


def test_fingerprint_is_deterministic_and_covers_exactly_the_documented_fields() -> None:
    payload = _payload()
    assert set(payload) == set(FINGERPRINT_FIELDS)
    assert payload["target_resource"] == "event:10"
    assert payload["actor_student_id"] == DEMO_STUDENT
    assert approval_fingerprint(payload) == approval_fingerprint(_payload())
    assert len(approval_fingerprint(payload)) == 64


@pytest.mark.parametrize(
    "overrides",
    [
        {"arguments": {"student_id": DEMO_STUDENT, "event_id": 11}},
        {"arguments": {"student_id": "STU-OTHER", "event_id": 10}},
        {"tool_name": "create_campus_case"},
        {"mission_id": "m-2"},
        {"step_id": "t-2"},
        {"precheck_status": "needs_review"},
    ],
)
def test_any_change_to_the_logical_action_changes_the_fingerprint(overrides) -> None:
    assert approval_fingerprint(_payload(**overrides)) != approval_fingerprint(_payload())


def test_fingerprint_ignores_extra_incidental_fields_and_key_order() -> None:
    payload = _payload()
    noisy = dict(reversed(list(payload.items())))
    noisy["created_at"] = utc_now().isoformat()
    noisy["approval_id"] = "appr-0123456789ab"
    assert approval_fingerprint(noisy) == approval_fingerprint(payload)


def test_arguments_are_normalized_through_the_tool_input_model() -> None:
    raw = {"event_id": "10", "student_id": DEMO_STUDENT}
    assert normalize_arguments(raw, RegisterEventInput) == {"student_id": DEMO_STUDENT, "event_id": 10}
    a = _payload(arguments=raw, input_model=RegisterEventInput)
    b = _payload(arguments={"student_id": DEMO_STUDENT, "event_id": 10}, input_model=RegisterEventInput)
    assert approval_fingerprint(a) == approval_fingerprint(b)


def test_fingerprint_is_stable_across_processes() -> None:
    """Never Python's per-process randomized hash(): a separate interpreter
    must compute the identical fingerprint."""
    code = (
        "from app.services.approval_binding import build_approval_payload, approval_fingerprint;"
        "print(approval_fingerprint(build_approval_payload(mission_id='m-1', step_id='t-1', "
        "tool_name='register_event', arguments={'student_id': 'STU-DEMO-001', 'event_id': 10}, "
        "precheck_status='verified')))"
    )
    other = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    assert other.stdout.strip() == approval_fingerprint(_payload())


def test_fingerprint_rejects_an_incomplete_payload() -> None:
    payload = _payload()
    del payload["arguments"]
    with pytest.raises(ValueError):
        approval_fingerprint(payload)


# ---------------------------------------------------------------------------
# A. normal approval: bound, executes exactly once
# ---------------------------------------------------------------------------


def test_a_valid_approval_is_bound_and_executes_exactly_once(seeded_session, knowledge_service) -> None:
    mission_id, task_id = "m-p11-a", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)
    approval = _approval(seeded_session, task_id)
    assert approval.payload_fingerprint == approval_fingerprint(approval.approved_payload)
    assert approval.approved_payload["target_resource"] == f"event:{CLEAN_EVENT_ID}"

    agent = _agent(seeded_session, knowledge_service)
    first = agent.handle(_message(mission_id, task_id))
    second = agent.handle(_message(mission_id, task_id))

    assert first.agent_result.status == AgentResultStatus.SUCCESS
    assert second.agent_result.facts["already_executed"] is True
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 1
    assert _approval(seeded_session, task_id).status == ApprovalStatus.APPROVED  # consumed, not stale


# ---------------------------------------------------------------------------
# B-D. state changes after approval -> STALE, zero writes
# ---------------------------------------------------------------------------


def test_b_schedule_change_after_approval_makes_the_approval_stale(seeded_session, knowledge_service) -> None:
    approval_id, outcome = _go_stale_on_schedule_change(seeded_session, knowledge_service, "m-p11-b")

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert outcome.agent_result.facts["approval_status"] == "stale"
    assert outcome.agent_result.facts["stale_approval_id"] == approval_id
    assert any("no longer valid" in e and "schedule conflict" in e for e in outcome.agent_result.errors)

    record = seeded_session.get(ApprovalRecord, approval_id)
    seeded_session.refresh(record)
    assert record.status == ApprovalStatus.STALE
    assert "schedule conflict" in record.invalidation_reason
    assert record.decision_by == "admin-demo" and record.decision_at is not None  # approver preserved
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 0
    tool_call = seeded_session.execute(
        select(ToolCallRecord).where(ToolCallRecord.tool_call_id == record.tool_call_id)
    ).scalar_one()
    assert tool_call.status == ToolExecutionStatus.FAILED and tool_call.executed_at is None


def test_c_event_filling_up_after_approval_makes_the_approval_stale(seeded_session, knowledge_service) -> None:
    mission_id, task_id = "m-p11-c", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)
    event = seeded_session.get(Event, CLEAN_EVENT_ID)
    event.capacity = len(
        seeded_session.execute(
            select(EventRegistration).where(
                EventRegistration.event_id == CLEAN_EVENT_ID, EventRegistration.status == RegistrationStatus.CONFIRMED
            )
        ).scalars().all()
    )
    seeded_session.commit()

    outcome = _agent(seeded_session, knowledge_service).handle(_message(mission_id, task_id))

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    approval = _approval(seeded_session, task_id)
    assert approval.status == ApprovalStatus.STALE
    assert "capacity" in approval.invalidation_reason.lower()
    assert {c["name"] for c in approval.invalidation_details["failed_checks"]} >= {"capacity_available"}
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 0


def test_d_deadline_passing_after_approval_makes_the_approval_stale(seeded_session, knowledge_service) -> None:
    mission_id, task_id = "m-p11-d", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)
    event = seeded_session.get(Event, CLEAN_EVENT_ID)
    event.registration_deadline = utc_now() - timedelta(hours=1)
    seeded_session.commit()

    outcome = _agent(seeded_session, knowledge_service).handle(_message(mission_id, task_id))

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    approval = _approval(seeded_session, task_id)
    assert approval.status == ApprovalStatus.STALE
    assert "deadline" in approval.invalidation_reason.lower()
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 0


def test_duplicate_registration_after_approval_makes_the_approval_stale(seeded_session, knowledge_service) -> None:
    from app.db.models.identity import Student

    mission_id, task_id = "m-p11-dup", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)
    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    seeded_session.add(EventRegistration(event_id=CLEAN_EVENT_ID, student_id=student.id, status=RegistrationStatus.CONFIRMED))
    seeded_session.commit()

    _agent(seeded_session, knowledge_service).handle(_message(mission_id, task_id))

    assert _approval(seeded_session, task_id).status == ApprovalStatus.STALE
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 1  # only the other process's row


# ---------------------------------------------------------------------------
# E. payload changed after approval
# ---------------------------------------------------------------------------


def test_e_tampered_payload_cannot_execute_under_the_old_approval(seeded_session, knowledge_service) -> None:
    """The approved tool call's arguments are changed after approval (e.g. a bug
    or a direct DB write). The fingerprint no longer matches, so the approval
    cannot authorize the new payload: blocked, STALE, nothing written."""
    mission_id, task_id = "m-p11-e", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)
    approval = _approval(seeded_session, task_id)
    tool_call = seeded_session.execute(
        select(ToolCallRecord).where(ToolCallRecord.tool_call_id == approval.tool_call_id)
    ).scalar_one()
    other_event_id = 6
    tool_call.arguments = {**tool_call.arguments, "event_id": other_event_id}
    seeded_session.commit()

    outcome = _agent(seeded_session, knowledge_service).handle(_message(mission_id, task_id))

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert any("fingerprint mismatch" in e for e in outcome.agent_result.errors)
    approval = _approval(seeded_session, task_id)
    assert approval.status == ApprovalStatus.STALE
    assert approval.invalidation_details["cause"] == "binding_mismatch"
    assert _registration_count(seeded_session, other_event_id) == 0
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 0


def test_e_edit_after_approval_requires_a_new_approval_with_a_new_fingerprint(seeded_session, knowledge_service) -> None:
    mission_id, task_id = "m-p11-e2", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)
    original = _approval(seeded_session, task_id)
    original_id, original_fp = original.approval_id, original.payload_fingerprint

    agent = _agent(seeded_session, knowledge_service)
    agent.propose_edit(
        mission_id, task_id, student_id=DEMO_STUDENT, requested_by="admin-demo",
        new_constraints={"tool_name": "register_event", "event_title": "Tech Talk: Cloud Native Systems"},
    )
    chain = _step_approvals(seeded_session, task_id)
    assert [a.status for a in chain] == [ApprovalStatus.EDIT_REQUIRED, ApprovalStatus.PENDING]
    assert chain[0].approval_id == original_id and chain[0].payload_fingerprint == original_fp
    assert chain[1].payload_fingerprint != original_fp  # one approval, one payload hash

    # The superseded approval can never be re-decided into authorizing anything.
    with pytest.raises(ApprovalGateError):
        ApprovalGate(seeded_session).decide(original_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
    # And the pending replacement never executes on its own.
    outcome = agent.handle(_message(mission_id, task_id))
    assert outcome.verification.status == VerificationStatus.NEEDS_REVIEW
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 0


def test_approval_fingerprint_is_immutable_once_requested(seeded_session, knowledge_service) -> None:
    mission_id, task_id = "m-p11-imm", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)
    fp_before = _approval(seeded_session, task_id).payload_fingerprint
    # Neither decide nor a status update path accepts or alters a fingerprint.
    context = ContextService(seeded_session)
    with pytest.raises(ValueError):
        context.update_approval_record(
            _approval(seeded_session, task_id).approval_id, ApprovalStatus.STALE, decision_by="x", decision_at=utc_now()
        )
    assert _approval(seeded_session, task_id).payload_fingerprint == fp_before


# ---------------------------------------------------------------------------
# F. stale approval unusable after a process restart
# ---------------------------------------------------------------------------

_RESTART_SCRIPT = r"""
import json, sys
from sqlalchemy import select
from app.agents.action.agent import ActionAgent
from app.db.models.events import EventRegistration
from app.db.models.identity import Student
from app.db.models.mission import ApprovalRecord
from app.db.session import create_db_engine, create_session_factory
from app.schemas.agent import AgentMessage
from app.schemas.enums import AgentName, ApprovalStatus
from app.services.approval_gate import ApprovalGate, ApprovalGateError
from app.tools.build import build_default_tool_registry

class NoEvidence:
    def search(self, *args, **kwargs):
        return []

db_path, approval_id, mission_id, task_id, event_id = sys.argv[1:6]
session = create_session_factory(create_db_engine(db_path=db_path))()
result = {"status_on_load": session.get(ApprovalRecord, approval_id).status.value}
try:
    ApprovalGate(session).decide(approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
    result["redecide"] = "accepted"
except ApprovalGateError:
    result["redecide"] = "refused"
agent = ActionAgent(session=session, knowledge_service=NoEvidence(), tool_gateway=build_default_tool_registry())
outcome = agent.handle(AgentMessage(
    message_id="msg-restart", mission_id=mission_id, task_id=task_id, source=AgentName.MISSION_ORCHESTRATOR,
    target=AgentName.ACTION_AGENT, objective="register", facts={"student_id": "STU-DEMO-001"},
    constraints={"tool_name": "register_event", "event_title": "Competitive Coding Contest"},
))
student = session.execute(select(Student).where(Student.student_code == "STU-DEMO-001")).scalar_one()
rows = session.execute(select(EventRegistration).where(
    EventRegistration.event_id == int(event_id), EventRegistration.student_id == student.id)).scalars().all()
session.expire_all()
result.update({
    "outcome": outcome.agent_result.status.value,
    "errors": outcome.agent_result.errors,
    "status_after": session.get(ApprovalRecord, approval_id).status.value,
    "registrations": len(rows),
})
print(json.dumps(result))
"""


def test_f_stale_approval_cannot_be_reused_after_a_process_restart(seeded_session, knowledge_service, engine) -> None:
    mission_id, task_id = "m-p11-f", "t-1"
    approval_id, _ = _go_stale_on_schedule_change(seeded_session, knowledge_service, mission_id, task_id)
    seeded_session.close()

    proc = subprocess.run(
        [sys.executable, "-c", _RESTART_SCRIPT, str(engine.url.database), approval_id, mission_id, task_id, str(CLEAN_EVENT_ID)],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["status_on_load"] == "stale"
    assert result["redecide"] == "refused"
    assert result["outcome"] == "failed"  # conflict still present: blocked at revalidation
    assert any("no longer valid" in e and "schedule conflict" in e for e in result["errors"])
    assert result["status_after"] == "stale"
    assert result["registrations"] == 0


# ---------------------------------------------------------------------------
# G-H. environment valid again -> NEW approval required; it executes once
# ---------------------------------------------------------------------------


def test_g_valid_again_still_requires_a_new_approval(seeded_session, knowledge_service) -> None:
    mission_id, task_id = "m-p11-g", "t-1"
    stale_id, _ = _go_stale_on_schedule_change(seeded_session, knowledge_service, mission_id, task_id)
    agent = _agent(seeded_session, knowledge_service)

    # Still invalid: re-dispatch is blocked by the current-state revalidation,
    # no replacement approval is created, nothing is written.
    blocked = agent.handle(_message(mission_id, task_id, constraints=REGISTER_CONSTRAINTS))
    assert blocked.agent_result.status == AgentResultStatus.FAILED
    assert blocked.agent_result.facts["revalidation_after_stale"] is True
    assert len(_step_approvals(seeded_session, task_id)) == 1

    _remove_rescheduled_exams(seeded_session)
    outcome = agent.handle(_message(mission_id, task_id, constraints=REGISTER_CONSTRAINTS))

    chain = _step_approvals(seeded_session, task_id)
    assert [a.status for a in chain] == [ApprovalStatus.STALE, ApprovalStatus.PENDING]
    assert chain[0].approval_id == stale_id
    assert outcome.verification.status == VerificationStatus.NEEDS_REVIEW  # paused for a human again
    assert outcome.agent_result.facts["replaces_approval_id"] == stale_id
    assert "needs a new approval" in outcome.response_text
    # The stale approval can never be revived.
    with pytest.raises(ApprovalGateError):
        ApprovalGate(seeded_session).decide(stale_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 0


def test_h_new_approval_executes_exactly_once(seeded_session, knowledge_service) -> None:
    mission_id, task_id = "m-p11-h", "t-1"
    stale_id, _ = _go_stale_on_schedule_change(seeded_session, knowledge_service, mission_id, task_id)
    _remove_rescheduled_exams(seeded_session)
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message(mission_id, task_id, constraints=REGISTER_CONSTRAINTS))
    new = _approval(seeded_session, task_id)
    assert new.approval_id != stale_id
    ApprovalGate(seeded_session).decide(new.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    first = agent.handle(_message(mission_id, task_id))
    again = agent.handle(_message(mission_id, task_id))

    assert first.agent_result.status == AgentResultStatus.SUCCESS
    assert first.agent_result.facts["postcondition_verified"] is True
    assert again.agent_result.facts["already_executed"] is True
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 1
    assert _successful_tool_calls(seeded_session, mission_id) == 1
    assert seeded_session.get(ApprovalRecord, stale_id).status == ApprovalStatus.STALE  # stale forever


# ---------------------------------------------------------------------------
# I. audit trail
# ---------------------------------------------------------------------------


def test_i_audit_trail_records_the_invalidation(seeded_session, knowledge_service) -> None:
    mission_id = "m-p11-i"
    approval_id, _ = _go_stale_on_schedule_change(seeded_session, knowledge_service, mission_id)

    events = _audit_events(seeded_session, mission_id, "approval_invalidated")
    assert len(events) == 1
    meta = events[0].event_metadata
    assert meta["approval_id"] == approval_id
    assert meta["approved_by"] == "admin-demo" and meta["approved_at"]
    assert "schedule conflict" in meta["reason"]
    assert meta["details"]["cause"] == "execution_recheck_failed"
    assert any(c["name"] == "no_schedule_conflicts" for c in meta["details"]["failed_checks"])
    assert meta["payload_fingerprint"]
    assert events[0].actor == "action_agent"
    # Recorded after (never instead of) the approval itself.
    types = [e.event_type for e in ContextService(seeded_session).list_audit_events(mission_id)]
    assert types.index("approval_approved") < types.index("approval_invalidated")
    assert "approval_rejected" not in types  # a stale approval is not a rejection
    requested = _audit_events(seeded_session, mission_id, "approval_requested")[0]
    assert requested.event_metadata["payload_fingerprint"] == meta["payload_fingerprint"]


# ---------------------------------------------------------------------------
# J + full mission flow through the Orchestrator
# ---------------------------------------------------------------------------


def test_j_repeated_resume_with_a_stale_approval_never_reattempts_execution(
    seeded_session, session_factory, knowledge_service
) -> None:
    orchestrator = _production_orchestrator(session_factory, knowledge_service)
    final = _run(orchestrator, REGISTER_GOAL)
    task_id = _action_task(final).task_id
    mission_id = final["mission_id"]
    _approve(session_factory, task_id)
    _add_exam_over_event(session_factory, CLEAN_EVENT_ID)

    for _ in range(3):
        result = orchestrator.resume_mission(mission_id)
        assert result["mission_status"] == MissionStatus.FAILED

    with session_factory() as session:
        chain = list_approvals_for_step(session, task_id)
        assert [a.status for a in chain] == [ApprovalStatus.STALE]  # no replacement while still invalid
        tool_calls = session.execute(select(ToolCallRecord).where(ToolCallRecord.mission_id == mission_id)).scalars().all()
        assert len(tool_calls) == 1 and tool_calls[0].status == ToolExecutionStatus.FAILED
        assert tool_calls[0].executed_at is None
        assert len(_audit_events(session, mission_id, "approval_invalidated")) == 1
    assert _registration_rows(session_factory, CLEAN_EVENT_ID) == 0


def test_mission_flow_stale_then_new_approval_then_single_execution(
    seeded_session, session_factory, knowledge_service
) -> None:
    orchestrator = _production_orchestrator(session_factory, knowledge_service)
    final = _run(orchestrator, REGISTER_GOAL)
    task_id, mission_id = _action_task(final).task_id, final["mission_id"]
    _approve(session_factory, task_id)
    _add_exam_over_event(session_factory, CLEAN_EVENT_ID)

    blocked = orchestrator.resume_mission(mission_id)
    assert blocked["mission_status"] == MissionStatus.FAILED
    with session_factory() as session:
        stale = get_latest_approval_for_step(session, task_id)
        assert stale.status == ApprovalStatus.STALE
        _remove_rescheduled_exams(session)

    replanned = orchestrator.resume_mission(mission_id)
    assert replanned["mission_status"] == MissionStatus.NEEDS_APPROVAL
    with session_factory() as session:
        chain = list_approvals_for_step(session, task_id)
        assert [a.status for a in chain] == [ApprovalStatus.STALE, ApprovalStatus.PENDING]
    _approve(session_factory, task_id)

    done = orchestrator.resume_mission(mission_id)
    assert done["mission_status"] == MissionStatus.COMPLETED
    assert done["agent_results"][task_id].facts["postcondition_verified"] is True
    assert _registration_rows(session_factory, CLEAN_EVENT_ID) == 1
    orchestrator.resume_mission(mission_id)  # a further resume is a no-op
    assert _registration_rows(session_factory, CLEAN_EVENT_ID) == 1


# ---------------------------------------------------------------------------
# Approval Gate / Context Service guards
# ---------------------------------------------------------------------------


def test_only_an_approved_approval_can_become_stale(session) -> None:
    context = ContextService(session)
    from app.schemas.enums import AgentName, UserRole

    context.create_mission("m-1", DEMO_STUDENT, UserRole.STUDENT, "goal")
    context.create_mission_step("t-1", "m-1", AgentName.ACTION_AGENT, "objective")
    gate = ApprovalGate(session)
    record = gate.request_approval(mission_id="m-1", step_id="t-1", tool_call_id=None, action_summary="x", requested_by="r")
    with pytest.raises(ApprovalGateError):
        gate.invalidate(record.approval_id, reason="changed", actor="action_agent")
    gate.decide(record.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
    stale = gate.invalidate(record.approval_id, reason="changed", actor="action_agent")
    assert stale.status == ApprovalStatus.STALE
    with pytest.raises(ApprovalGateError):
        gate.invalidate(record.approval_id, reason="again", actor="action_agent")
    with pytest.raises(ApprovalGateError):
        gate.mark_superseded(record.approval_id, requested_by="admin-demo")
    with pytest.raises(ValueError):
        context.update_approval_record(record.approval_id, ApprovalStatus.APPROVED, decision_by="x", decision_at=utc_now())


def test_legacy_approval_without_fingerprint_never_executes(seeded_session, knowledge_service) -> None:
    """An approval persisted before Phase 11 has no binding: blocked + STALE."""
    mission_id, task_id = "m-p11-legacy", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)
    approval = _approval(seeded_session, task_id)
    approval.payload_fingerprint = None
    approval.approved_payload = None
    seeded_session.commit()

    outcome = _agent(seeded_session, knowledge_service).handle(_message(mission_id, task_id))

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert _approval(seeded_session, task_id).status == ApprovalStatus.STALE
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 0


# ---------------------------------------------------------------------------
# additive schema upgrade for databases created before Phase 11
# ---------------------------------------------------------------------------


def test_upgrade_schema_adds_the_approval_binding_columns_to_an_old_database(tmp_path) -> None:
    db_file = tmp_path / "old.db"
    old = create_engine(f"sqlite:///{db_file.as_posix()}")
    with old.begin() as conn:
        conn.execute(text(
            "CREATE TABLE approval_records (approval_id VARCHAR(64) PRIMARY KEY, mission_id VARCHAR(64), "
            "step_id VARCHAR(64), tool_call_id VARCHAR(64), action_summary TEXT, requested_by VARCHAR(120), "
            "status VARCHAR(13), decision_by VARCHAR(120), decision_at DATETIME, decision_reason TEXT, created_at DATETIME)"
        ))
        conn.execute(text("INSERT INTO approval_records (approval_id, status) VALUES ('appr-old', 'approved')"))
    old.dispose()

    engine = create_db_engine(db_path=str(db_file))
    added = upgrade_schema(engine)
    assert {"approval_records.payload_fingerprint", "approval_records.invalidation_reason"} <= set(added)
    assert upgrade_schema(engine) == []  # idempotent
    columns = {c["name"] for c in inspect(engine).get_columns("approval_records")}
    assert {"payload_fingerprint", "approved_payload", "invalidated_at", "invalidation_reason", "invalidation_details"} <= columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM approval_records")).scalar_one() == "approved"  # data untouched
    engine.dispose()


_IMPORT_THEN_START_SCRIPT = r"""
import json, sqlite3, sys
db = sys.argv[1]
columns = lambda: [r[1] for r in sqlite3.connect(db).execute("pragma table_info(approval_records)")]
import app.api.main as main  # builds the default app against CAMPUSNEXUS_DB_PATH
after_import = columns()
from fastapi.testclient import TestClient
with TestClient(main.app):
    pass
print(json.dumps({"after_import": after_import, "after_startup": columns()}))
"""


def test_api_upgrades_the_schema_at_startup_never_at_import(tmp_path) -> None:
    """Tests import app.api.main; importing must never alter whatever DB the
    environment points at (the dev DB, outside tests). Server startup does."""
    import os

    db_file = tmp_path / "old.db"
    old = create_engine(f"sqlite:///{db_file.as_posix()}")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE approval_records (approval_id VARCHAR(64) PRIMARY KEY, status VARCHAR(13))"))
    old.dispose()

    env = {**os.environ, "CAMPUSNEXUS_DB_PATH": str(db_file)}
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_THEN_START_SCRIPT, str(db_file)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True,
    )
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "payload_fingerprint" not in result["after_import"]
    assert "payload_fingerprint" in result["after_startup"]


# ---------------------------------------------------------------------------
# API / UI
# ---------------------------------------------------------------------------

STUDENT = {"X-Demo-Identity": "student-demo"}
ADMIN = {"X-Demo-Identity": "admin-demo"}
STALE_TEXT = "Approval expired because execution conditions changed. Review the updated action and approve again."


def _api_mission_to_stale(api_client, session_factory) -> tuple:
    mission = api_client.post("/missions", json={"goal": REGISTER_GOAL}, headers=STUDENT).json()
    approval = next(a for a in api_client.get("/approvals/pending", headers=ADMIN).json() if a["mission_id"] == mission["mission_id"])
    assert api_client.post(f"/approvals/{approval['approval_id']}/decision", json={"decision": "approve"}, headers=ADMIN).status_code == 200
    _add_exam_over_event(session_factory, CLEAN_EVENT_ID)
    api_client.post(f"/missions/{mission['mission_id']}/resume", headers=STUDENT)
    return mission["mission_id"], approval["approval_id"]


def test_api_exposes_the_stale_approval_distinct_from_a_rejection(api_client, session_factory) -> None:
    mission_id, approval_id = _api_mission_to_stale(api_client, session_factory)

    stale = api_client.get("/approvals/stale", headers=ADMIN).json()
    assert [a["approval_id"] for a in stale] == [approval_id]
    view = stale[0]
    assert view["status"] == "stale"
    assert view["status_message"] == STALE_TEXT
    assert view["decision_by"] and view["decision_at"]  # the human approval is still on record
    assert "schedule conflict" in view["invalidation_reason"]
    assert view["payload_fingerprint"] and view["invalidated_at"]
    assert view["replaced_by_approval_id"] is None
    assert api_client.get("/approvals/stale", headers={"X-Demo-Identity": "student-alt"}).json() == []

    # Re-deciding a stale approval is refused with the expiry explanation.
    again = api_client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers=ADMIN)
    assert again.status_code == 409 and "expired" in again.json()["detail"]

    mission = api_client.get(f"/missions/{mission_id}", headers=STUDENT).json()
    assert [a["approval_id"] for a in mission["stale_approvals"]] == [approval_id]
    assert mission["pending_approvals"] == []
    timeline = api_client.get(f"/missions/{mission_id}/timeline", headers=STUDENT).json()
    invalidated = [e for e in timeline["entries"] if e["event_type"] == "approval_invalidated"]
    assert len(invalidated) == 1 and invalidated[0]["status"] == "NEEDS_REVIEW"


def test_api_links_the_replacement_request_to_the_expired_one(api_client, session_factory) -> None:
    mission_id, stale_id = _api_mission_to_stale(api_client, session_factory)
    with session_factory() as session:
        _remove_rescheduled_exams(session)
    api_client.post(f"/missions/{mission_id}/resume", headers=STUDENT)

    pending = [a for a in api_client.get("/approvals/pending", headers=ADMIN).json() if a["mission_id"] == mission_id]
    assert len(pending) == 1
    new = pending[0]
    assert new["approval_id"] != stale_id
    assert new["replaces_approval_id"] == stale_id
    assert new["payload_fingerprint"]
    assert "replacing expired request" in new["status_message"]
    stale = api_client.get("/approvals/stale", headers=ADMIN).json()[0]
    assert stale["replaced_by_approval_id"] == new["approval_id"]


def test_timeline_maps_invalidation_to_needs_review_not_failed() -> None:
    class _Event:
        event_id = "evt-1"
        event_type = "approval_invalidated"
        message = "Approval appr-1 is now STALE"
        timestamp = utc_now()
        step_id = "t-1"
        event_metadata = {}
        actor = "action_agent"

    entries = build_timeline([], [_Event()])
    assert entries[-1].status == "NEEDS_REVIEW"


def test_action_center_renders_expired_approval_without_calling_it_rejected(api_app, session_factory) -> None:
    from fastapi.testclient import TestClient
    from streamlit.testing.v1 import AppTest

    from streamlit_app import api_client as api_client_module

    with TestClient(api_app) as client:
        api_client_module.set_default_http_client(client)
        try:
            _api_mission_to_stale(client, session_factory)
            at = AppTest.from_file(str(REPO_ROOT / "streamlit_app" / "app.py"))
            at.run(timeout=30)
            at.sidebar.selectbox[0].set_value("admin-demo").run(timeout=30)
            at.sidebar.radio[0].set_value("Action Center").run(timeout=30)
            assert not at.exception
            warnings = [w.value for w in at.warning]
            assert any(STALE_TEXT in w for w in warnings)
            body = "\n".join(md.value for md in at.markdown)
            assert ">STALE<" in body
            assert "REJECTED" not in body and "rejected" not in "\n".join(warnings).lower()
        finally:
            api_client_module.set_default_http_client(None)
