"""The ``register_event`` write tool.

CLAUDE.md Idempotency Requirement: ``EventRegistration`` already carries a
``UniqueConstraint(event_id, student_id)`` (app/db/models/events.py) -- this
handler's own pre-insert lookup is the fast path, and the
``IntegrityError`` catch is the race-safe fallback if two dispatches somehow
land concurrently, so a retried registration can never create a duplicate
row. Capacity/open-status are rechecked immediately before commit, since
they can have changed since the Action Agent's own pre-action check ran.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.events import EventRegistration, EventStatus, RegistrationStatus
from app.db.models.identity import Student
from app.db.repositories.events import count_confirmed_registrations, get_event, get_registration
from app.schemas.action import RegisterEventInput
from app.schemas.enums import ToolExecutionStatus
from app.schemas.tools import ToolResult


def register_event(session: Session, data: RegisterEventInput, tool_call_id: str) -> ToolResult:
    event = get_event(session, data.event_id)
    if event is None:
        return ToolResult(tool_call_id=tool_call_id, status=ToolExecutionStatus.FAILED, error=f"unknown event_id {data.event_id}")

    existing = get_registration(session, data.event_id, data.student_id)
    if existing is not None:
        return ToolResult(
            tool_call_id=tool_call_id,
            status=ToolExecutionStatus.SUCCESS,
            data={"registration_id": existing.id, "status": existing.status.value, "already_existed": True},
        )

    if event.status != EventStatus.OPEN:
        return ToolResult(
            tool_call_id=tool_call_id, status=ToolExecutionStatus.FAILED, error=f"event is not open (status={event.status.value})"
        )
    if event.capacity is not None and count_confirmed_registrations(session, event.id) >= event.capacity:
        return ToolResult(tool_call_id=tool_call_id, status=ToolExecutionStatus.FAILED, error="event is at capacity")

    student = session.execute(select(Student).where(Student.student_code == data.student_id)).scalar_one_or_none()
    if student is None:
        return ToolResult(tool_call_id=tool_call_id, status=ToolExecutionStatus.FAILED, error=f"unknown student_id {data.student_id}")

    registration = EventRegistration(event_id=event.id, student_id=student.id, status=RegistrationStatus.CONFIRMED)
    session.add(registration)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = get_registration(session, data.event_id, data.student_id)
        if existing is not None:
            return ToolResult(
                tool_call_id=tool_call_id,
                status=ToolExecutionStatus.SUCCESS,
                data={"registration_id": existing.id, "status": existing.status.value, "already_existed": True},
            )
        raise
    session.refresh(registration)
    return ToolResult(
        tool_call_id=tool_call_id,
        status=ToolExecutionStatus.SUCCESS,
        data={"registration_id": registration.id, "status": registration.status.value, "already_existed": False},
    )
