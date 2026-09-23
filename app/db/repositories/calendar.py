"""Read-only calendar repository functions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.communication import CalendarEvent
from app.db.models.identity import Student


def get_student_calendar(session: Session, student_code: str) -> list[CalendarEvent]:
    stmt = (
        select(CalendarEvent)
        .join(Student, CalendarEvent.student_id == Student.id)
        .where(Student.student_code == student_code)
        .order_by(CalendarEvent.start_at)
    )
    return list(session.execute(stmt).scalars().all())


def get_calendar_entry(
    session: Session, student_code: str, title: str, start_at: datetime
) -> CalendarEvent | None:
    """Exact-match lookup (student, title, start_at) -- the duplicate-entry dedup key."""
    stmt = (
        select(CalendarEvent)
        .join(Student, CalendarEvent.student_id == Student.id)
        .where(
            Student.student_code == student_code,
            CalendarEvent.title == title,
            CalendarEvent.start_at == start_at,
        )
    )
    return session.execute(stmt).scalar_one_or_none()
