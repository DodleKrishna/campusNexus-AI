"""Identity and campus structure: departments, users, students."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, portable_enum, UTCDateTime, utc_now
from app.schemas.enums import UserRole


class Department(Base):
    """An academic department (e.g. Computer Science)."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    # Phase 17 (nullable, additive): the faculty profile that heads this department.
    # HOD authority is resolved from this record, never from the account role alone.
    hod_faculty_id: Mapped[Optional[int]] = mapped_column(ForeignKey("faculty_profiles.id", use_alter=True), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    students: Mapped[List["Student"]] = relationship(back_populates="department")


class User(Base):
    """A campus identity (student, faculty, staff, or admin)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[UserRole] = mapped_column(
        portable_enum(UserRole)
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    student: Mapped[Optional["Student"]] = relationship(back_populates="user", uselist=False)


class Student(Base):
    """Student-specific profile data layered on top of a User identity.

    ``student_code`` is the stable, human-referenceable natural key (e.g.
    "STU-DEMO-001") that future agents and the Context Service address a
    student by -- the surrogate integer ``id`` stays an internal DB detail.
    """

    __tablename__ = "students"
    __table_args__ = (UniqueConstraint("user_id", name="uq_students_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    year: Mapped[int]
    semester: Mapped[int]
    cgpa: Mapped[float]
    interests: Mapped[Optional[str]] = mapped_column(String(500), default=None)
    career_goal: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    # Phase 16 (nullable, additive): class section within year/semester, and the
    # faculty mentor who reviews requests no specific class faculty owns.
    section: Mapped[Optional[str]] = mapped_column(String(10), default=None)
    mentor_faculty_id: Mapped[Optional[int]] = mapped_column(ForeignKey("faculty_profiles.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    user: Mapped["User"] = relationship(back_populates="student")
    department: Mapped["Department"] = relationship(back_populates="students")
