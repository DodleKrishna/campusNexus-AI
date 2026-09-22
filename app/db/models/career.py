"""Career domain: companies, opportunities, skills, applications.

Eligibility criteria (allowed departments, eligible years, required skills)
are represented relationally via join tables rather than CSV/JSON blobs, so a
later deterministic eligibility rule can query them directly. The eligibility
*algorithm* itself is out of scope for this phase (app/rules/, later).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Enum as SAEnum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, utc_now


class OpportunityType(str, enum.Enum):
    INTERNSHIP = "internship"
    JOB = "job"


class OpportunityStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"


class ApplicationStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    industry: Mapped[str] = mapped_column(String(100))
    website: Mapped[Optional[str]] = mapped_column(String(255), default=None)


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)


class StudentSkill(Base):
    """A skill a student has, with self-reported proficiency 1 (novice) - 5 (expert)."""

    __tablename__ = "student_skills"
    __table_args__ = (UniqueConstraint("student_id", "skill_id", name="uq_student_skills_student_skill"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"))
    proficiency: Mapped[int]

    student = relationship("Student")
    skill = relationship("Skill")


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    title: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text)
    opportunity_type: Mapped[OpportunityType] = mapped_column(
        SAEnum(OpportunityType, values_callable=lambda e: [m.value for m in e])
    )
    minimum_cgpa: Mapped[float]
    deadline: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[OpportunityStatus] = mapped_column(
        SAEnum(OpportunityStatus, values_callable=lambda e: [m.value for m in e])
    )
    posted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    company = relationship("Company")
    allowed_departments: Mapped[List["OpportunityDepartment"]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )
    eligible_years: Mapped[List["OpportunityEligibleYear"]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )
    required_skills: Mapped[List["OpportunitySkill"]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )


class OpportunityDepartment(Base):
    """One row per department allowed to apply to an opportunity."""

    __tablename__ = "opportunity_departments"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "department_id", name="uq_opportunity_departments_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))

    opportunity = relationship("Opportunity", back_populates="allowed_departments")
    department = relationship("Department")


class OpportunityEligibleYear(Base):
    """One row per student year (1-4) eligible for an opportunity."""

    __tablename__ = "opportunity_eligible_years"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "year", name="uq_opportunity_eligible_years_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"))
    year: Mapped[int]

    opportunity = relationship("Opportunity", back_populates="eligible_years")


class OpportunitySkill(Base):
    """A skill required (with an optional minimum proficiency) by an opportunity."""

    __tablename__ = "opportunity_skills"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "skill_id", name="uq_opportunity_skills_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"))
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"))
    minimum_proficiency: Mapped[Optional[int]] = mapped_column(default=None)

    opportunity = relationship("Opportunity", back_populates="required_skills")
    skill = relationship("Skill")


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("student_id", "opportunity_id", name="uq_applications_student_opportunity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"))
    status: Mapped[ApplicationStatus] = mapped_column(
        SAEnum(ApplicationStatus, values_callable=lambda e: [m.value for m in e]),
        default=ApplicationStatus.SUBMITTED,
    )
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    student = relationship("Student")
    opportunity = relationship("Opportunity")
