"""Read-only events repository functions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.events import Event, EventRegistration, EventStatus
from app.db.models.identity import Student


def list_upcoming_events(session: Session, now: datetime) -> list[Event]:
    """Events that start at or after ``now`` (caller supplies UTC ``now`` explicitly
    to keep this function pure/deterministic and avoid hidden clock reads)."""
    stmt = select(Event).where(Event.start_at >= now).order_by(Event.start_at)
    return list(session.execute(stmt).scalars().all())


def get_event(session: Session, event_id: int) -> Event | None:
    stmt = select(Event).where(Event.id == event_id)
    return session.execute(stmt).scalar_one_or_none()


def get_student_registrations(session: Session, student_code: str) -> list[EventRegistration]:
    stmt = (
        select(EventRegistration)
        .join(Student, EventRegistration.student_id == Student.id)
        .where(Student.student_code == student_code)
    )
    return list(session.execute(stmt).scalars().all())
