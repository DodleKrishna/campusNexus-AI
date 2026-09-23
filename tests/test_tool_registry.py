"""Tool Gateway tests: the enforcement point CLAUDE.md's Tool Security Rules
describe (unknown tool, role/ownership, unvalidated arguments, idempotency,
transaction safety). Uses the real seeded DB (student STU-DEMO-001, event
id 2 = "Hackathon Kickoff Session", open/not-full/not-yet-registered).
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models.events import EventRegistration
from app.db.models.identity import Student
from app.schemas.enums import ToolAccessType, ToolExecutionStatus, UserRole
from app.schemas.tools import ToolCall
from app.tools.build import build_default_tool_registry
from app.tools.registry import ToolDefinition, ToolGateway, UnknownToolError

DEMO_STUDENT = "STU-DEMO-001"


def _write_call(tool_name: str, arguments: dict, *, idempotency_key: str, tool_call_id: str = "tc-1") -> ToolCall:
    return ToolCall(
        tool_call_id=tool_call_id, mission_id="m-1", task_id="t-1", tool_name=tool_name,
        arguments=arguments, read_or_write=ToolAccessType.WRITE, requires_approval=True,
        idempotency_key=idempotency_key,
    )


def test_unknown_tool_name_fails_without_raising(seeded_session) -> None:
    gateway = build_default_tool_registry()
    call = _write_call("delete_everything", {"student_id": DEMO_STUDENT}, idempotency_key="k1")
    result = gateway.execute(seeded_session, call, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)
    assert result.status == ToolExecutionStatus.FAILED
    assert "No tool registered" in result.error


def test_get_unknown_tool_raises() -> None:
    gateway = ToolGateway()
    try:
        gateway.get("nope")
        assert False, "expected UnknownToolError"
    except UnknownToolError:
        pass


def test_invalid_arguments_fail_validation_before_handler_runs(seeded_session) -> None:
    gateway = build_default_tool_registry()
    call = _write_call("register_event", {"student_id": DEMO_STUDENT, "event_id": "not-an-int"}, idempotency_key="k2")
    result = gateway.execute(seeded_session, call, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)
    assert result.status == ToolExecutionStatus.FAILED
    assert "invalid arguments" in result.error


def test_ownership_mismatch_is_rejected_even_with_valid_arguments(seeded_session) -> None:
    gateway = build_default_tool_registry()
    call = _write_call("register_event", {"student_id": "STU2023002", "event_id": 2}, idempotency_key="k3")
    result = gateway.execute(seeded_session, call, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)
    assert result.status == ToolExecutionStatus.FAILED
    assert "ownership check failed" in result.error

    # No row should have been created for the mismatched target student.
    student = seeded_session.execute(select(Student).where(Student.student_code == "STU2023002")).scalar_one()
    rows = seeded_session.execute(
        select(EventRegistration).where(EventRegistration.event_id == 2, EventRegistration.student_id == student.id)
    ).scalars().all()
    assert rows == []


def test_role_not_authorized_is_rejected(seeded_session) -> None:
    gateway = build_default_tool_registry()
    call = _write_call("register_event", {"student_id": DEMO_STUDENT, "event_id": 2}, idempotency_key="k4")
    result = gateway.execute(seeded_session, call, caller_role=UserRole.FACULTY, caller_student_id=DEMO_STUDENT)
    assert result.status == ToolExecutionStatus.FAILED
    assert "not authorized" in result.error


def test_successful_execution_creates_a_real_row(seeded_session) -> None:
    gateway = build_default_tool_registry()
    call = _write_call("register_event", {"student_id": DEMO_STUDENT, "event_id": 2}, idempotency_key="k5")
    result = gateway.execute(seeded_session, call, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)
    assert result.status == ToolExecutionStatus.SUCCESS
    assert result.data["already_existed"] is False

    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    rows = seeded_session.execute(
        select(EventRegistration).where(EventRegistration.event_id == 2, EventRegistration.student_id == student.id)
    ).scalars().all()
    assert len(rows) == 1


def test_idempotent_retry_never_creates_a_duplicate_row(seeded_session) -> None:
    gateway = build_default_tool_registry()
    call1 = _write_call("register_event", {"student_id": DEMO_STUDENT, "event_id": 3}, idempotency_key="k-retry", tool_call_id="tc-a")
    call2 = _write_call("register_event", {"student_id": DEMO_STUDENT, "event_id": 3}, idempotency_key="k-retry", tool_call_id="tc-b")

    result1 = gateway.execute(seeded_session, call1, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)
    result2 = gateway.execute(seeded_session, call2, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)

    assert result1.status == ToolExecutionStatus.SUCCESS
    assert result2.status == ToolExecutionStatus.SUCCESS
    assert result1.data["registration_id"] == result2.data["registration_id"]

    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    rows = seeded_session.execute(
        select(EventRegistration).where(EventRegistration.event_id == 3, EventRegistration.student_id == student.id)
    ).scalars().all()
    assert len(rows) == 1


def test_natural_duplicate_key_is_returned_not_duplicated_even_with_a_new_idempotency_key(seeded_session) -> None:
    """A second, *different* mission step somehow re-registering the same
    (event, student) pair -- e.g. a stale/incorrect plan -- must still hit
    EventRegistration's own UniqueConstraint and come back as the existing
    row, never a second row or an unhandled crash."""
    gateway = build_default_tool_registry()
    call1 = _write_call("register_event", {"student_id": DEMO_STUDENT, "event_id": 10}, idempotency_key="k-a", tool_call_id="tc-c")
    call2 = _write_call("register_event", {"student_id": DEMO_STUDENT, "event_id": 10}, idempotency_key="k-b", tool_call_id="tc-d")

    result1 = gateway.execute(seeded_session, call1, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)
    result2 = gateway.execute(seeded_session, call2, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)

    assert result1.status == ToolExecutionStatus.SUCCESS
    assert result2.status == ToolExecutionStatus.SUCCESS
    assert result2.data["already_existed"] is True

    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    rows = seeded_session.execute(
        select(EventRegistration).where(EventRegistration.event_id == 10, EventRegistration.student_id == student.id)
    ).scalars().all()
    assert len(rows) == 1


def test_handler_exception_becomes_a_failed_result_and_session_stays_usable(seeded_session) -> None:
    def _boom(session, data, tool_call_id):
        raise RuntimeError("simulated handler crash")

    gateway = ToolGateway()
    gateway.register(
        ToolDefinition(
            name="register_event",
            input_model=build_default_tool_registry().get("register_event").input_model,
            authorized_roles=frozenset({UserRole.STUDENT}),
            required_permission="events:register_self",
            requires_approval=True,
            idempotency_strategy="n/a",
            postconditions="n/a",
            handler=_boom,
        )
    )
    call = _write_call("register_event", {"student_id": DEMO_STUDENT, "event_id": 2}, idempotency_key="k-boom")
    result = gateway.execute(seeded_session, call, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)
    assert result.status == ToolExecutionStatus.FAILED
    assert "RuntimeError" in result.error

    # The session must still be usable afterward (rollback happened cleanly).
    seeded_session.execute(select(Student)).scalars().all()
