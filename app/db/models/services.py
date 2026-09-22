"""Campus Services domain: cases (hostel/fees/facilities/IT/admin), SLAs."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Enum as SAEnum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, utc_now


class CaseStatus(str, enum.Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class CasePriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class CampusCase(Base):
    __tablename__ = "campus_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    category: Mapped[str] = mapped_column(String(60))  # hostel | fees | facilities | it_helpdesk | administrative
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[CasePriority] = mapped_column(
        SAEnum(CasePriority, values_callable=lambda e: [m.value for m in e])
    )
    department: Mapped[str] = mapped_column(String(80))
    status: Mapped[CaseStatus] = mapped_column(
        SAEnum(CaseStatus, values_callable=lambda e: [m.value for m in e]),
        default=CaseStatus.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    student = relationship("Student")
    assignments: Mapped[list["CaseAssignment"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    sla: Mapped[Optional["CaseSLA"]] = relationship(
        back_populates="case", uselist=False, cascade="all, delete-orphan"
    )


class CaseAssignment(Base):
    __tablename__ = "case_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("campus_cases.id"))
    assignee_name: Mapped[str] = mapped_column(String(120))
    assigned_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    case = relationship("CampusCase", back_populates="assignments")


class CaseSLA(Base):
    """Deterministic SLA targets/actuals for one case (breach = past-due, unresolved)."""

    __tablename__ = "case_slas"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("campus_cases.id"), unique=True)
    response_due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolution_due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    responded_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)

    case = relationship("CampusCase", back_populates="sla")
