"""Identity and campus structure: departments, users, students."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, portable_enum, UTCDateTime, utc_now, TenantMixin
from app.schemas.enums import UserRole


class Department(TenantMixin, Base):
    """An academic department (e.g. Computer Science)."""

    __tablename__ = "departments"
    __table_args__ = (Index("ux_departments_org_code", "organization_id", "code", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), index=True)  # unique per organization (Phase 22B)
    name: Mapped[str] = mapped_column(String(120))
    # Phase 17 (nullable, additive): the faculty profile that heads this department.
    # HOD authority is resolved from this record, never from the account role alone.
    hod_faculty_id: Mapped[Optional[int]] = mapped_column(ForeignKey("faculty_profiles.id", use_alter=True), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    students: Mapped[List["Student"]] = relationship(back_populates="department")


class User(TenantMixin, Base):
    """A campus identity (student, faculty, staff, or admin)."""

    __tablename__ = "users"
    __table_args__ = (Index("ux_users_org_email", "organization_id", "email", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)  # unique per organization (Phase 22B)
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[UserRole] = mapped_column(
        portable_enum(UserRole)
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    student: Mapped[Optional["Student"]] = relationship(back_populates="user", uselist=False)


class Student(TenantMixin, Base):
    """Student-specific profile data layered on top of a User identity.

    ``student_code`` is the stable, human-referenceable natural key (e.g.
    "STU-DEMO-001") that future agents and the Context Service address a
    student by -- the surrogate integer ``id`` stays an internal DB detail.
    """

    __tablename__ = "students"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_students_user_id"),
        # Phase 22 (D1): student_code stays globally unique for now (legacy FKs point at it), but every
        # application lookup is by organization + student_code.
        Index("ix_students_org_student_code", "organization_id", "student_code"),
    )

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
