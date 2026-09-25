"""Phase 22: organizations (tenants) and account memberships.

An ``Organization`` is one institution. Its data lives in the shared tables,
each row carrying ``organization_id`` (``app.db.base.TenantMixin``); there is
no schema per tenant.

``auth_accounts`` stays the *global* login identity. What an account may do in
an organization -- its authoritative role and its role-specific profile -- is
an ``OrganizationMembership``:

    student         -> student_id (students.id)
    faculty / hod   -> faculty_profile_id (faculty_profiles.id)
    admin / staff   -> no profile link

``auth_accounts.role`` is only a compatibility mirror of the membership role.
Phase 22 allows exactly one active membership per account (so sign-in needs no
organization picker); the table itself already allows memberships in several
organizations for later.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, portable_enum, UTCDateTime, utc_now
from app.schemas.enums import MembershipStatus, OrganizationStatus, UserRole

# The role <-> profile-link rule, enforced by the database as well as by the code that writes memberships.
MEMBERSHIP_PROFILE_RULE = (
    "(role = 'student' AND student_id IS NOT NULL AND faculty_profile_id IS NULL) OR "
    "(role IN ('faculty', 'hod') AND faculty_profile_id IS NOT NULL AND student_id IS NULL) OR "
    "(role IN ('admin', 'staff') AND student_id IS NULL AND faculty_profile_id IS NULL)"
)
_ACTIVE = f"status = '{MembershipStatus.ACTIVE.value}'"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[OrganizationStatus] = mapped_column(portable_enum(OrganizationStatus), default=OrganizationStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    # Monthly AI budget in (estimated) USD. NULL = no limit configured. Exhausted -> AI_BUDGET_EXCEEDED.
    monthly_ai_budget_usd: Mapped[Optional[float]] = mapped_column(Float, default=None)


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "account_id", name="uq_organization_memberships_org_account"),
        CheckConstraint(MEMBERSHIP_PROFILE_RULE, name="role_profile_link"),
        # Phase 22: one active membership per account. Partial index: inactive memberships may pile up.
        Index("ux_organization_memberships_active_account", "account_id", unique=True,
              sqlite_where=text(_ACTIVE), postgresql_where=text(_ACTIVE)),
        Index("ux_organization_memberships_org_student", "organization_id", "student_id", unique=True),
        Index("ux_organization_memberships_org_faculty", "organization_id", "faculty_profile_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("auth_accounts.id"), index=True)
    role: Mapped[UserRole] = mapped_column(portable_enum(UserRole))
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("students.id"), default=None)
    faculty_profile_id: Mapped[Optional[int]] = mapped_column(ForeignKey("faculty_profiles.id"), default=None)
    status: Mapped[MembershipStatus] = mapped_column(portable_enum(MembershipStatus), default=MembershipStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)

    organization = relationship("Organization")
    account = relationship("AuthAccount")
