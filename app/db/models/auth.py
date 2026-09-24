"""Phase 15: login credentials for the React application.

Kept separate from ``users`` (the campus *identity* table the seed data and
every agent already rely on): an ``AuthAccount`` is only a way to sign in and
be authorized. A STUDENT account points at its student by the stable
``student_code``; the server derives every student-scoped query from that
link, never from a client-supplied id. The password is only ever stored as a
bcrypt hash.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, utc_now
from app.schemas.enums import UserRole


class AuthAccount(Base):
    __tablename__ = "auth_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole, values_callable=lambda e: [m.value for m in e]))
    display_name: Mapped[str] = mapped_column(String(120))
    linked_student_id: Mapped[Optional[str]] = mapped_column(ForeignKey("students.student_code"), default=None)
    # Phase 16: faculty_profiles.id for a FACULTY/HOD account. Faculty-scoped
    # endpoints derive the faculty only from this link.
    linked_faculty_id: Mapped[Optional[int]] = mapped_column(default=None)
    department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id"), default=None)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)

    department = relationship("Department")
