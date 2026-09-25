"""Events domain: clubs, events, registrations."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, portable_enum, UTCDateTime, utc_now


class EventStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class RegistrationStatus(str, enum.Enum):
    CONFIRMED = "confirmed"
    WAITLISTED = "waitlisted"
    CANCELLED = "cancelled"


class Club(Base):
    __tablename__ = "clubs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60))


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    club_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clubs.id"), default=None)
    title: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60))
    organizer: Mapped[str] = mapped_column(String(120))
    location: Mapped[str] = mapped_column(String(120))
    start_at: Mapped[datetime] = mapped_column(UTCDateTime)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime)
    registration_deadline: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    capacity: Mapped[Optional[int]] = mapped_column(default=None)
    status: Mapped[EventStatus] = mapped_column(
        portable_enum(EventStatus)
    )

    club = relationship("Club")


class EventRegistration(Base):
    """A student's registration for an event.

    Unique (event_id, student_id) is the structural basis for future
    duplicate-registration prevention / post-condition verification.
    """

    __tablename__ = "event_registrations"
    __table_args__ = (UniqueConstraint("event_id", "student_id", name="uq_event_registrations_event_student"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    status: Mapped[RegistrationStatus] = mapped_column(
        portable_enum(RegistrationStatus),
        default=RegistrationStatus.CONFIRMED,
    )
    registered_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    event = relationship("Event")
    student = relationship("Student")
