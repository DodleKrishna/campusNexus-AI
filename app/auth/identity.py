"""Phase 22B: who is calling, and in which organization -- resolved only from the database.

``TenantContext`` is built exclusively by this module, from a signed token (or
a server-side demo identity) plus a fresh database lookup. Nothing a client, an
LLM, a plan or a tool argument supplies can create or change it.

* ``login_membership`` -- at sign-in, the account's single active membership.
  The only cross-organization lookup in the runtime, marked ``IDENTITY_LOOKUP``
  so the tenant session allows it (identity models only).
* ``resolve_identity`` -- per request: binds the session to the token's
  organization and verifies account, membership, organization and the
  role-specific profile in ONE statement. Any mismatch (other organization,
  other membership, inactive account/membership/organization, missing profile)
  resolves to None, which the API turns into 401.

The authoritative role is ``organization_memberships.role``; ``auth_accounts.role``
is never consulted here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.db.models.auth import AuthAccount
from app.db.models.faculty import FacultyProfile
from app.db.models.identity import Student
from app.db.models.organization import Organization, OrganizationMembership
from app.db.tenant_session import IDENTITY_LOOKUP, bind_organization
from app.schemas.enums import MembershipStatus, OrganizationStatus, UserRole


@dataclass(frozen=True)
class TenantContext:
    """The server-owned tenant identity of one request. Immutable; never built from client input."""

    organization_id: int
    membership_id: Optional[int]  # None only for the Phase 8 X-Demo-Identity allowlist (no account behind it)
    account_id: Optional[int]
    role: UserRole


@dataclass(frozen=True)
class ResolvedIdentity:
    context: TenantContext
    account: AuthAccount
    membership: OrganizationMembership
    organization: Organization
    student: Optional[Student]
    faculty: Optional[FacultyProfile]


def login_membership(session: Session, account: AuthAccount) -> Optional[OrganizationMembership]:
    """The account's single active membership in an active organization (Phase 22: at most one)."""
    return session.execute(
        select(OrganizationMembership)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.account_id == account.id,
            OrganizationMembership.status == MembershipStatus.ACTIVE,
            Organization.status == OrganizationStatus.ACTIVE,
        )
        .execution_options(**{IDENTITY_LOOKUP: True})
    ).scalar_one_or_none()


def resolve_identity(session: Session, *, account_id: int, organization_id: int, membership_id: int) -> Optional[ResolvedIdentity]:
    """Bind ``session`` to ``organization_id`` and verify the whole identity in one statement."""
    bind_organization(session, organization_id)
    row = session.execute(
        select(AuthAccount, OrganizationMembership, Organization, Student, FacultyProfile)
        .join(OrganizationMembership, and_(OrganizationMembership.account_id == AuthAccount.id,
                                           OrganizationMembership.id == membership_id))
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .outerjoin(Student, Student.id == OrganizationMembership.student_id)
        .outerjoin(FacultyProfile, FacultyProfile.id == OrganizationMembership.faculty_profile_id)
        .where(
            AuthAccount.id == account_id,
            AuthAccount.is_active.is_(True),
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.status == MembershipStatus.ACTIVE,
            Organization.status == OrganizationStatus.ACTIVE,
        )
    ).one_or_none()
    if row is None:
        return None
    account, membership, organization, student, faculty = row
    role = membership.role
    # The role-specific profile must exist in this same organization (the outer joins are organization-scoped).
    if role == UserRole.STUDENT and (student is None or student.organization_id != organization_id):
        return None
    if role in (UserRole.FACULTY, UserRole.HOD) and (faculty is None or faculty.organization_id != organization_id):
        return None
    context = TenantContext(organization_id=organization_id, membership_id=membership.id, account_id=account.id, role=role)
    return ResolvedIdentity(context=context, account=account, membership=membership, organization=organization,
                            student=student, faculty=faculty)


def demo_organization(session: Session, slug: str) -> Optional[Organization]:
    """The fixed organization of a Phase 8 server-side demo identity (never chosen by the client)."""
    return session.execute(
        select(Organization).where(Organization.slug == slug, Organization.status == OrganizationStatus.ACTIVE)
    ).scalar_one_or_none()
