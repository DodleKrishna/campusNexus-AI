"""Communication domain: notifications and calendar entries.

Lightweight persistence only -- actually sending a notification or writing to
an external calendar is Action Agent behavior for a later phase.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, portable_enum, UTCDateTime, utc_now


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
        portable_enum(NotificationStatus),
        default=NotificationStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    sent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)

    student = relationship("Student")


class StaffNotification(Base):
    """Phase 17: an in-app notification for a faculty member or HOD.

    ``notifications`` belongs to students (``student_id`` is required), so staff
    get their own table. ``ref_key`` de-duplicates generated warnings (for example
    one delayed-class warning per class meeting).
    """

    __tablename__ = "staff_notifications"
    __table_args__ = (UniqueConstraint("faculty_id", "ref_key", name="uq_staff_notifications_faculty_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty_profiles.id"), index=True)
    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60))
    status: Mapped[NotificationStatus] = mapped_column(
        portable_enum(NotificationStatus),
        default=NotificationStatus.SENT,
    )
    ref_key: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class AccountNotification(Base):
    """Phase 18: an in-app notification for an account with no faculty/student profile (administrators)."""

    __tablename__ = "account_notifications"
    __table_args__ = (UniqueConstraint("account_id", "ref_key", name="uq_account_notifications_account_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("auth_accounts.id"), index=True)
    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60))
    status: Mapped[NotificationStatus] = mapped_column(
        portable_enum(NotificationStatus),
        default=NotificationStatus.SENT,
    )
    ref_key: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


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
