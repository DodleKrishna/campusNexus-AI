"""Read-only events repository functions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.events import Event, EventRegistration, EventStatus, RegistrationStatus
from app.db.models.identity import Student


def count_confirmed_registrations(session: Session, event_id: int) -> int:
    """Number of CONFIRMED registrations for an event -- the capacity-check input.

    WAITLISTED/CANCELLED registrations don't count against capacity.
    """
    stmt = select(func.count()).select_from(EventRegistration).where(
        EventRegistration.event_id == event_id, EventRegistration.status == RegistrationStatus.CONFIRMED
    )
    return session.execute(stmt).scalar_one()


def list_upcoming_events(session: Session, now: datetime) -> list[Event]:
    """Events that start at or after ``now`` (caller supplies UTC ``now`` explicitly
    to keep this function pure/deterministic and avoid hidden clock reads)."""
    stmt = select(Event).where(Event.start_at >= now).order_by(Event.start_at)
    return list(session.execute(stmt).scalars().all())


def get_event(session: Session, event_id: int) -> Event | None:
    stmt = select(Event).where(Event.id == event_id)
    return session.execute(stmt).scalar_one_or_none()


def get_event_by_title(session: Session, title: str) -> Event | None:
    stmt = select(Event).where(Event.title == title)
    return session.execute(stmt).scalar_one_or_none()


def get_student_registrations(session: Session, student_code: str) -> list[EventRegistration]:
    stmt = (
        select(EventRegistration)
        .join(Student, EventRegistration.student_id == Student.id)
        .where(Student.student_code == student_code)
    )
    return list(session.execute(stmt).scalars().all())


def get_registration(session: Session, event_id: int, student_code: str) -> EventRegistration | None:
    """The student's own registration row for one event, or None -- the
    duplicate-registration precondition/postcondition check's direct read."""
    stmt = (
        select(EventRegistration)
        .join(Student, EventRegistration.student_id == Student.id)
        .where(EventRegistration.event_id == event_id, Student.student_code == student_code)
    )
    return session.execute(stmt).scalar_one_or_none()
