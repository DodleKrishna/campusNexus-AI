"""The Events Service: read-only tools over the Phase 2 events repositories
(app/db/repositories/events.py). Mirrors app/services/academic.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.repositories.events import (
    count_confirmed_registrations,
    get_event,
    get_event_by_title,
    get_student_registrations,
    list_upcoming_events,
)
from app.schemas.events import EventSummary


def _event_summary(event) -> EventSummary:
    return EventSummary(
        event_id=event.id,
        title=event.title,
        description=event.description,
        category=event.category,
        organizer=event.organizer,
        location=event.location,
        start_at=event.start_at,
        end_at=event.end_at,
        registration_deadline=event.registration_deadline,
        capacity=event.capacity,
        status=event.status.value,
    )


def get_upcoming_events(session: Session, now: datetime) -> List[EventSummary]:
    return [_event_summary(e) for e in list_upcoming_events(session, now)]


def get_event_summary(session: Session, event_id: int) -> Optional[EventSummary]:
    event = get_event(session, event_id)
    return _event_summary(event) if event is not None else None


def get_event_summary_by_title(session: Session, title: str) -> Optional[EventSummary]:
    event = get_event_by_title(session, title)
    return _event_summary(event) if event is not None else None


def get_registration_count(session: Session, event_id: int) -> int:
    """Confirmed registration count for one event -- the capacity-check input."""
    return count_confirmed_registrations(session, event_id)


def get_student_registration_status(session: Session, student_id: str, event_id: int) -> Optional[str]:
    """The student's own registration status for one event, or None if not registered."""
    for registration in get_student_registrations(session, student_id):
        if registration.event_id == event_id:
            return registration.status.value
    return None
