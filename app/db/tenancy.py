"""Phase 22A: organization data helpers -- the default organization, backfill, memberships, integrity.

These are data-level operations for seeding, migration and integrity checks.
They are not request-time tenant isolation (that is Phase 22B's tenant session).

* ``ensure_default_organization`` creates the single demo institution.
* ``assign_unowned_rows`` gives every row without an organization to that
  organization. It refuses when the database holds more than one organization:
  there, a row without an owner is a bug to investigate, never something to
  attribute by guessing.
* ``sync_memberships`` creates the membership for each account that has none,
  from the account's role and profile links. The membership role is
  authoritative from then on; ``auth_accounts.role`` is only its mirror, kept in
  step by ``set_account_role``.
* ``tenancy_report`` lists what is not yet consistent: rows without an
  organization, rows whose organization differs from a row they reference,
  accounts without an active membership, and role/profile-link drift between an
  account and its membership.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Table, and_, func, select, update
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models.auth import AuthAccount
from app.db.models.identity import Student
from app.db.models.organization import Organization, OrganizationMembership
from app.schemas.enums import MembershipStatus, OrganizationStatus, UserRole

DEFAULT_ORGANIZATION_SLUG = "campusnexus-demo"
DEFAULT_ORGANIZATION_NAME = "CampusNexus Demo Institution"
ORGANIZATION_COLUMN = "organization_id"


class TenancyError(RuntimeError):
    """A tenancy operation that would have to guess which organization owns data."""


@dataclass
class MembershipSync:
    created: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (email, reason)


@dataclass
class TenancyReport:
    unowned_rows: Dict[str, int] = field(default_factory=dict)
    cross_tenant_links: Dict[str, int] = field(default_factory=dict)  # "child.column -> parent": rows
    accounts_without_membership: List[str] = field(default_factory=list)
    membership_drift: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.unowned_rows or self.cross_tenant_links or self.accounts_without_membership or self.membership_drift)

    def lines(self) -> List[str]:
        out = [f"rows without an organization: {sum(self.unowned_rows.values())}"
               + (f" ({', '.join(f'{t}={n}' for t, n in sorted(self.unowned_rows.items()))})" if self.unowned_rows else "")]
        out.append(f"cross-organization references: {sum(self.cross_tenant_links.values())}"
                   + (f" ({', '.join(f'{k}={n}' for k, n in sorted(self.cross_tenant_links.items()))})" if self.cross_tenant_links else ""))
        out.append(f"accounts without an active membership: {len(self.accounts_without_membership)}"
                   + (f" ({', '.join(self.accounts_without_membership)})" if self.accounts_without_membership else ""))
        out.append(f"membership drift: {len(self.membership_drift)}" + (f" ({'; '.join(self.membership_drift)})" if self.membership_drift else ""))
        return out


def tenant_tables() -> List[Table]:
    """Every table that carries ``organization_id``, parents before children."""
    return [t for t in Base.metadata.sorted_tables if ORGANIZATION_COLUMN in t.c]


def ensure_default_organization(session: Session) -> Organization:
    organization = session.execute(
        select(Organization).where(Organization.slug == DEFAULT_ORGANIZATION_SLUG)
    ).scalar_one_or_none()
    if organization is None:
        organization = Organization(slug=DEFAULT_ORGANIZATION_SLUG, name=DEFAULT_ORGANIZATION_NAME, status=OrganizationStatus.ACTIVE)
        session.add(organization)
        session.flush()
    return organization


def assign_unowned_rows(session: Session, organization: Organization) -> Dict[str, int]:
    """Give every row without an organization to ``organization``. Only in a single-organization database."""
    organizations = session.execute(select(func.count()).select_from(Organization)).scalar_one()
    if organizations != 1:
        raise TenancyError(
            f"refusing to assign rows without an organization: the database holds {organizations} organizations, "
            "so their owner cannot be inferred"
        )
    session.flush()
    assigned: Dict[str, int] = {}
    for table in tenant_tables():
        column = table.c[ORGANIZATION_COLUMN]
        result = session.execute(update(table).where(column.is_(None)).values({ORGANIZATION_COLUMN: organization.id}))
        if result.rowcount:
            assigned[table.name] = result.rowcount
    return assigned


def _profile_links(session: Session, account: AuthAccount) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """(student_id, faculty_profile_id, problem) the account's role requires, from its legacy links."""
    role = account.role
    if role == UserRole.STUDENT:
        if not account.linked_student_id or account.linked_faculty_id:
            return None, None, "a student account needs exactly one student link"
        student_id = session.execute(
            select(Student.id).where(Student.student_code == account.linked_student_id)
        ).scalar_one_or_none()
        return (student_id, None, None) if student_id is not None else (None, None, "its student does not exist")
    if role in (UserRole.FACULTY, UserRole.HOD):
        if not account.linked_faculty_id or account.linked_student_id:
            return None, None, f"a {role.value} account needs exactly one faculty profile link"
        return None, account.linked_faculty_id, None
    if account.linked_student_id or account.linked_faculty_id:
        return None, None, f"an {role.value} account must not be linked to a student or faculty profile"
    return None, None, None


def sync_memberships(session: Session, organization: Organization) -> MembershipSync:
    """Create the membership of each account that has none (idempotent). Existing memberships are never changed."""
    result = MembershipSync()
    member_ids = set(session.execute(select(OrganizationMembership.account_id)).scalars())
    for account in session.execute(select(AuthAccount).order_by(AuthAccount.id)).scalars():
        if account.id in member_ids:
            continue
        student_id, faculty_profile_id, problem = _profile_links(session, account)
        if problem:
            result.skipped.append((account.email, problem))
            continue
        session.add(OrganizationMembership(
            organization_id=organization.id, account_id=account.id, role=account.role, student_id=student_id,
            faculty_profile_id=faculty_profile_id, status=MembershipStatus.ACTIVE,
        ))
        result.created.append(account.email)
    session.flush()
    return result


def active_membership(session: Session, account_id: int) -> Optional[OrganizationMembership]:
    return session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.account_id == account_id, OrganizationMembership.status == MembershipStatus.ACTIVE
        )
    ).scalar_one_or_none()


def set_account_role(session: Session, account: AuthAccount, role: UserRole) -> None:
    """Change an account's role: the membership (authoritative) and the ``auth_accounts.role`` mirror together."""
    membership = active_membership(session, account.id)
    if membership is not None:
        membership.role = role
    account.role = role


def tenancy_report(session: Session) -> TenancyReport:
    report = TenancyReport()
    tables = tenant_tables()
    tenant_names = {t.name for t in tables}
    for table in tables:
        column = table.c[ORGANIZATION_COLUMN]
        unowned = session.execute(select(func.count()).select_from(table).where(column.is_(None))).scalar_one()
        if unowned:
            report.unowned_rows[table.name] = unowned
        for constraint in table.foreign_key_constraints:
            parent = constraint.referred_table
            if parent.name not in tenant_names or len(constraint.elements) != 1:
                continue
            element = constraint.elements[0]
            child_column = table.c[element.parent.name]
            referenced = parent.alias() if parent is table else parent
            parent_column = referenced.c[element.column.name]
            mismatched = session.execute(
                select(func.count()).select_from(table).join(referenced, child_column == parent_column).where(
                    and_(column.is_not(None), referenced.c[ORGANIZATION_COLUMN].is_not(None),
                         column != referenced.c[ORGANIZATION_COLUMN])
                )
            ).scalar_one()
            if mismatched:
                report.cross_tenant_links[f"{table.name}.{child_column.name} -> {parent.name}"] = mismatched
    memberships = {m.account_id: m for m in session.execute(
        select(OrganizationMembership).where(OrganizationMembership.status == MembershipStatus.ACTIVE)
    ).scalars()}
    for account in session.execute(select(AuthAccount).order_by(AuthAccount.id)).scalars():
        membership = memberships.get(account.id)
        if membership is None:
            report.accounts_without_membership.append(account.email)
            continue
        if membership.role != account.role:
            report.membership_drift.append(f"{account.email}: membership role {membership.role.value}, account mirror {account.role.value}")
        student_id, faculty_profile_id, problem = _profile_links(session, account)
        if problem is None and (membership.student_id, membership.faculty_profile_id) != (student_id, faculty_profile_id):
            report.membership_drift.append(f"{account.email}: membership profile link differs from the account's")
    return report


def admin_account_ids(session: Session) -> List[int]:
    """Active accounts whose active membership is ADMIN (Phase 22B: the membership role is authoritative).

    In a tenant session the membership is organization-scoped, so this is the admins of that organization only.
    """
    return list(session.execute(
        select(AuthAccount.id)
        .join(OrganizationMembership, OrganizationMembership.account_id == AuthAccount.id)
        .where(OrganizationMembership.role == UserRole.ADMIN, OrganizationMembership.status == MembershipStatus.ACTIVE,
               AuthAccount.is_active.is_(True))
        .order_by(AuthAccount.id)
    ).scalars())
