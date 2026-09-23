"""The ``create_calendar_event`` write tool.

No DB-level uniqueness constraint exists on ``CalendarEvent`` (it's a free-
form personal entry, unlike ``EventRegistration``'s natural key), so
idempotency here rests on an explicit pre-insert dedup lookup on
(student, title, start_at) -- the same key
``app/db/repositories/calendar.py::get_calendar_entry`` uses for the
duplicate-entry precondition check.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.communication import CalendarEvent
from app.db.models.identity import Student
from app.db.repositories.calendar import get_calendar_entry
from app.schemas.action import CreateCalendarEventInput
from app.schemas.enums import ToolExecutionStatus
from app.schemas.tools import ToolResult


def create_calendar_event(session: Session, data: CreateCalendarEventInput, tool_call_id: str) -> ToolResult:
    student = session.execute(select(Student).where(Student.student_code == data.student_id)).scalar_one_or_none()
    if student is None:
        return ToolResult(tool_call_id=tool_call_id, status=ToolExecutionStatus.FAILED, error=f"unknown student_id {data.student_id}")

    existing = get_calendar_entry(session, data.student_id, data.title, data.start_at)
    if existing is not None:
        return ToolResult(
            tool_call_id=tool_call_id,
            status=ToolExecutionStatus.SUCCESS,
            data={"calendar_event_id": existing.id, "already_existed": True},
        )

    entry = CalendarEvent(
        student_id=student.id,
        title=data.title,
        start_at=data.start_at,
        end_at=data.end_at,
        source_type=data.source_type,
        source_id=data.source_id,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return ToolResult(
        tool_call_id=tool_call_id,
        status=ToolExecutionStatus.SUCCESS,
        data={"calendar_event_id": entry.id, "already_existed": False},
    )
