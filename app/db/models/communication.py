"""Communication domain: notifications and calendar entries.

Lightweight persistence only -- actually sending a notification or writing to
an external calendar is Action Agent behavior for a later phase.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Enum as SAEnum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, utc_now


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    READ = "read"


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60))
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus, values_callable=lambda e: [m.value for m in e]),
        default=NotificationStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    sent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)

    student = relationship("Student")


class CalendarEvent(Base):
    """A student-facing calendar entry, e.g. mirroring an exam or event."""

    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    title: Mapped[str] = mapped_column(String(150))
    start_at: Mapped[datetime] = mapped_column(UTCDateTime)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source_type: Mapped[str] = mapped_column(String(30))  # "event" | "exam" | "deadline"
    source_id: Mapped[Optional[int]] = mapped_column(default=None)

    student = relationship("Student")
