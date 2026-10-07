"""Phase 22A -- organizations, memberships, the tenant column, seeding and the migration.

Runs on SQLite; with CAMPUSNEXUS_TEST_ALL_ON_POSTGRES=1 the fixture-based tests
run on PostgreSQL too, and the legacy-migration test has its own PostgreSQL
variant (throwaway schema).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import Column, Engine, ForeignKey, MetaData, Table, func, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from app.auth.accounts import seed_dev_accounts
from app.db.base import NAMING_CONVENTION, Base
from app.db.models.auth import AuthAccount
from app.db.models.faculty import FacultyProfile
from app.db.models.identity import Student
from app.db.models.organization import Organization, OrganizationMembership
from app.db.session import create_db_engine, create_session_factory, init_db, upgrade_schema
from app.db.tenancy import (
    DEFAULT_ORGANIZATION_SLUG,
    TenancyError,
    assign_unowned_rows,
    tenancy_report,
    tenant_tables,
)
from app.schemas.enums import MembershipStatus, UserRole
from app.services import admin_ops
from scripts import migrate_phase22_tenancy
from scripts.seed_data import run_seed
from tests.pg_support import postgres_schema_engine, requires_postgres

GLOBAL_TABLES = {"organizations", "auth_accounts"}
NOW = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def seeded(session):
    run_seed(session)
    seed_dev_accounts(session, "phase22a-test-password")
    return session


def _account(session, email: str) -> AuthAccount:
    return session.execute(select(AuthAccount).where(AuthAccount.email == email)).scalar_one()


def _membership(session, account: AuthAccount) -> OrganizationMembership:
    return session.execute(select(OrganizationMembership).where(OrganizationMembership.account_id == account.id)).scalar_one()


# --- Structure ---------------------------------------------------------------------------------------------


def test_every_table_except_organizations_and_auth_accounts_is_tenant_owned() -> None:
    tables = Base.metadata.tables
    owned = {t.name for t in tenant_tables()}
    assert owned == set(tables) - GLOBAL_TABLES
    for name in owned:
        column = tables[name].c.organization_id
        assert [fk.column.table.name for fk in column.foreign_keys] == ["organizations"], name
        assert any(ix.columns.keys()[0] == "organization_id" for ix in tables[name].indexes), f"{name}: organization_id is not indexed"
    assert "organization_id" not in tables["auth_accounts"].c  # the global login identity


def test_student_code_is_looked_up_per_organization_but_stays_globally_unique_for_now() -> None:
    students = Base.metadata.tables["students"]
    assert students.c.student_code.unique  # D1: legacy global uniqueness kept in Phase 22
    assert any(ix.name == "ix_students_org_student_code" and ix.columns.keys() == ["organization_id", "student_code"]
               for ix in students.indexes)


def test_a_new_database_has_the_tenant_schema(engine) -> None:
    """The live schema (SQLite, or PostgreSQL with CAMPUSNEXUS_TEST_ALL_ON_POSTGRES=1), not just the models."""
    inspector = inspect(engine)
    assert {"organizations", "organization_memberships"} <= set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        columns = {c["name"] for c in inspector.get_columns(table.name)}
        foreign_keys = inspector.get_foreign_keys(table.name)
        if table.name in GLOBAL_TABLES:
            assert "organization_id" not in columns, table.name
            continue
        assert "organization_id" in columns, table.name
        assert any(fk["referred_table"] == "organizations" and fk["constrained_columns"] == ["organization_id"]
                   for fk in foreign_keys), f"{table.name}: no foreign key to organizations"
    unique = {ix["name"] for t in ("departments", "users", "courses", "faculty_profiles", "clubs", "companies", "skills",
                                   "organization_memberships") for ix in inspector.get_indexes(t) if ix["unique"]}
    assert {"ux_departments_org_code", "ux_users_org_email", "ux_courses_org_code", "ux_faculty_profiles_org_employee_code",
            "ux_clubs_org_name", "ux_companies_org_name", "ux_skills_org_name",
            "ux_organization_memberships_active_account"} <= unique
    assert "ix_students_org_student_code" in {ix["name"] for ix in inspector.get_indexes("students")}
    assert upgrade_schema(engine) == []  # a new database needs no upgrade


# --- Seed ------------------------------------------------------------------------------------------------


def test_the_seed_puts_every_row_and_account_in_the_demo_organization(seeded) -> None:
    organizations = seeded.execute(select(Organization)).scalars().all()
    assert [o.slug for o in organizations] == [DEFAULT_ORGANIZATION_SLUG]
    report = tenancy_report(seeded)
    assert report.ok, report.lines()
    accounts = seeded.execute(select(func.count()).select_from(AuthAccount)).scalar_one()
    active = seeded.execute(select(func.count()).select_from(OrganizationMembership).where(
        OrganizationMembership.status == MembershipStatus.ACTIVE)).scalar_one()
    assert accounts == active == 7


def test_memberships_link_the_role_specific_profile(seeded) -> None:
    student = _membership(seeded, _account(seeded, "student@campusnexus.local"))
    assert student.role == UserRole.STUDENT and student.faculty_profile_id is None
    assert seeded.get(Student, student.student_id).student_code == "STU-DEMO-001"
    for email, role, employee_code in (("faculty@campusnexus.local", UserRole.FACULTY, "EMP-CSE-004"),
                                       ("hod@campusnexus.local", UserRole.HOD, "EMP-CSE-001")):
        membership = _membership(seeded, _account(seeded, email))
        assert membership.role == role and membership.student_id is None
        assert seeded.get(FacultyProfile, membership.faculty_profile_id).employee_code == employee_code
    admin = _membership(seeded, _account(seeded, "admin@campusnexus.local"))
    assert admin.role == UserRole.ADMIN and admin.student_id is None and admin.faculty_profile_id is None


def test_seeding_twice_changes_nothing(seeded) -> None:
    before = {t.name: seeded.execute(select(func.count()).select_from(t)).scalar_one() for t in tenant_tables()}
    run_seed(seeded)
    seed_dev_accounts(seeded, "phase22a-test-password")
    after = {t.name: seeded.execute(select(func.count()).select_from(t)).scalar_one() for t in tenant_tables()}
    assert before == after and tenancy_report(seeded).ok


# --- Membership constraints --------------------------------------------------------------------------------


@pytest.mark.parametrize("role, link", [
    (UserRole.STUDENT, {}),                                   # a student must link a student
    (UserRole.STUDENT, {"faculty": True}),                    # ... and never a faculty profile
    (UserRole.FACULTY, {}),                                   # faculty must link a faculty profile
    (UserRole.HOD, {"student": True, "faculty": True}),      # never both
    (UserRole.ADMIN, {"faculty": True}),                      # admin links no profile
])
def test_the_database_enforces_the_role_profile_rule(seeded, role, link) -> None:
    organization = seeded.execute(select(Organization)).scalar_one()
    account = AuthAccount(email=f"rule-{role.value}@example.test", password_hash="x", role=role, display_name="Rule")
    seeded.add(account)
    seeded.flush()
    seeded.add(OrganizationMembership(
        organization_id=organization.id, account_id=account.id, role=role,
        student_id=seeded.execute(select(Student.id).limit(1)).scalar_one() if link.get("student") else None,
        faculty_profile_id=seeded.execute(select(FacultyProfile.id).limit(1)).scalar_one() if link.get("faculty") else None,
    ))
    with pytest.raises(IntegrityError):
        seeded.flush()
    seeded.rollback()


def test_an_account_has_at_most_one_active_membership(seeded) -> None:
    admin = _account(seeded, "admin@campusnexus.local")
    other = Organization(slug="northfield-demo", name="Northfield Demo")
    seeded.add(other)
    seeded.flush()
    seeded.add(OrganizationMembership(organization_id=other.id, account_id=admin.id, role=UserRole.ADMIN,
                                      status=MembershipStatus.ACTIVE))
    with pytest.raises(IntegrityError):
        seeded.flush()
    seeded.rollback()
    # An inactive membership elsewhere is allowed: the table is ready for multi-organization accounts later.
    other = Organization(slug="northfield-demo", name="Northfield Demo")
    seeded.add(other)
    seeded.flush()
    seeded.add(OrganizationMembership(organization_id=other.id, account_id=admin.id, role=UserRole.ADMIN,
                                      status=MembershipStatus.INACTIVE))
    seeded.flush()


def test_a_profile_backs_at_most_one_membership_per_organization(seeded) -> None:
    faculty = _membership(seeded, _account(seeded, "faculty@campusnexus.local"))
    account = AuthAccount(email="second-login@example.test", password_hash="x", role=UserRole.FACULTY, display_name="Twin")
    seeded.add(account)
    seeded.flush()
    seeded.add(OrganizationMembership(organization_id=faculty.organization_id, account_id=account.id, role=UserRole.FACULTY,
                                      faculty_profile_id=faculty.faculty_profile_id))
    with pytest.raises(IntegrityError):
        seeded.flush()
    seeded.rollback()


def test_a_role_change_updates_the_membership_and_its_mirror(seeded) -> None:
    admin = _account(seeded, "admin@campusnexus.local")
    hod = _account(seeded, "hod@campusnexus.local")
    admin_ops.change_role(seeded, admin, hod.id, UserRole.FACULTY.value, NOW)
    seeded.expire_all()
    assert _membership(seeded, hod).role == UserRole.FACULTY  # authoritative
    assert _account(seeded, "hod@campusnexus.local").role == UserRole.FACULTY  # compatibility mirror
    report = tenancy_report(seeded)
    assert report.membership_drift == [] and report.accounts_without_membership == []
    # 22A: runtime writes (here the role-change audit event) do not set an organization yet; the Phase 22B
    # tenant session does. Until then a re-run of scripts/migrate_phase22_tenancy.py assigns them.
    assert report.unowned_rows == {"operation_audit_events": 1}


# --- Backfill safety and integrity ---------------------------------------------------------------------------


def test_rows_without_an_organization_are_never_attributed_when_several_exist(seeded) -> None:
    seeded.add(Organization(slug="northfield-demo", name="Northfield Demo"))
    seeded.flush()
    student = seeded.execute(select(Student).limit(1)).scalar_one()
    student.organization_id = None
    seeded.flush()
    with pytest.raises(TenancyError):
        assign_unowned_rows(seeded, seeded.execute(select(Organization).where(Organization.slug == DEFAULT_ORGANIZATION_SLUG)).scalar_one())
    seeded.rollback()


def test_the_report_finds_rows_that_reference_another_organization(seeded) -> None:
    other = Organization(slug="northfield-demo", name="Northfield Demo")
    seeded.add(other)
    seeded.flush()
    enrollments = Base.metadata.tables["enrollments"]
    first = seeded.execute(select(enrollments.c.id).order_by(enrollments.c.id).limit(1)).scalar_one()
    seeded.execute(update(enrollments).where(enrollments.c.id == first).values(organization_id=other.id))
    report = tenancy_report(seeded)
    assert not report.ok
    assert report.cross_tenant_links.get("enrollments.student_id -> students") == 1
    assert report.cross_tenant_links.get("enrollments.course_id -> courses") == 1
    seeded.rollback()


# --- upgrade_schema and the migration of a database from before Phase 22 ------------------------------------


def test_upgrade_schema_adds_the_organization_column_with_its_foreign_key_and_index(tmp_path) -> None:
    db_file = tmp_path / "old.db"
    engine = create_db_engine(db_path=str(db_file))
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE clubs (id INTEGER PRIMARY KEY, name VARCHAR(120), description TEXT, category VARCHAR(60))"))
        connection.execute(text("INSERT INTO clubs (id, name) VALUES (1, 'Coding Club')"))
    added = upgrade_schema(engine)
    assert "organizations" in added and "clubs.organization_id" in added
    assert {"index:ix_clubs_organization_id", "index:ux_clubs_org_name"} <= set(added)
    foreign_keys = inspect(engine).get_foreign_keys("clubs")
    assert any(fk["referred_table"] == "organizations" and fk["constrained_columns"] == ["organization_id"] for fk in foreign_keys)
    assert upgrade_schema(engine) == []  # idempotent
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name, organization_id FROM clubs")).one() == ("Coding Club", None)
    engine.dispose()


def build_legacy_database(target: Engine, source: Engine,
                          exclude: tuple[str, ...] = ("organizations", "organization_memberships")) -> dict[str, int]:
    """The pre-Phase-22 schema (no organization tables, no organization_id) holding ``source``'s rows.

    ``exclude`` names the tables the older schema did not have yet.
    """
    legacy = MetaData(naming_convention=NAMING_CONVENTION)
    for table in Base.metadata.sorted_tables:
        if table.name in exclude:
            continue
        columns = [
            Column(c.name, c.type, *[ForeignKey(fk.target_fullname, use_alter=fk.use_alter) for fk in c.foreign_keys],
                   primary_key=c.primary_key, nullable=c.nullable, unique=bool(c.unique))
            for c in table.columns if c.name != "organization_id"
            # A column referencing a table the older schema did not have was added later (e.g. exams.guardian_mission_id).
            and not any(fk.column.table.name in exclude for fk in c.foreign_keys)
        ]
        Table(table.name, legacy, *columns)
    legacy.create_all(target)
    counts: dict[str, int] = {}
    deferred = []  # the departments <-> faculty_profiles cycle: insert NULL, restore afterwards
    with source.connect() as src, target.begin() as dst:
        for table in legacy.sorted_tables:
            cycle = [c.name for c in table.columns if any(fk.use_alter for fk in c.foreign_keys)]
            rows = [dict(r) for r in src.execute(select(*[Base.metadata.tables[table.name].c[c.name] for c in table.columns])).mappings()]
            for row in rows:
                for name in cycle:
                    if row[name] is not None:
                        deferred.append((table, row["id"], name, row[name]))
                        row[name] = None
            if rows:
                dst.execute(insert(table), rows)
            counts[table.name] = len(rows)
        for table, row_id, name, value in deferred:
            dst.execute(update(table).where(table.c.id == row_id).values({name: value}))
    return counts


@pytest.fixture()
def seeded_source(tmp_path) -> Engine:
    source = create_db_engine(db_path=str(tmp_path / "current.db"))
    init_db(source)
    with create_session_factory(source)() as session:
        run_seed(session)
        seed_dev_accounts(session, "phase22a-test-password")
    yield source
    source.dispose()


def _assert_migrates(legacy: Engine, counts: dict[str, int], capsys) -> None:
    assert migrate_phase22_tenancy.plan(legacy) == 0  # dry run: reads only
    assert "organizations" not in inspect(legacy).get_table_names()
    assert migrate_phase22_tenancy.migrate(legacy) == 0
    with create_session_factory(legacy)() as session:
        report = tenancy_report(session)
        assert report.ok, report.lines()
        assert [o.slug for o in session.execute(select(Organization)).scalars()] == [DEFAULT_ORGANIZATION_SLUG]
        for name, expected in counts.items():  # nothing lost or duplicated
            assert session.execute(select(func.count()).select_from(Base.metadata.tables[name])).scalar_one() == expected, name
        assert session.execute(select(func.count()).select_from(OrganizationMembership)).scalar_one() == counts["auth_accounts"]
    assert migrate_phase22_tenancy.migrate(legacy) == 0  # re-run: a no-op
    assert "Rows assigned: 0" in capsys.readouterr().out


def test_a_database_from_before_phase_22_migrates_completely(tmp_path, seeded_source, capsys) -> None:
    legacy = create_db_engine(db_path=str(tmp_path / "legacy.db"))
    counts = build_legacy_database(legacy, seeded_source)
    _assert_migrates(legacy, counts, capsys)
    legacy.dispose()


def test_the_migration_stops_on_an_account_it_cannot_map(tmp_path, seeded_source, capsys) -> None:
    legacy = create_db_engine(db_path=str(tmp_path / "legacy.db"))
    build_legacy_database(legacy, seeded_source)
    with legacy.begin() as connection:  # an admin wrongly linked to a faculty profile
        connection.execute(text("UPDATE auth_accounts SET linked_faculty_id = 1 WHERE email = 'admin@campusnexus.local'"))
    assert migrate_phase22_tenancy.plan(legacy) == 1
    assert migrate_phase22_tenancy.migrate(legacy) == 1
    assert "ROLLED BACK" in capsys.readouterr().out
    with legacy.connect() as connection:  # the backfill transaction left nothing behind
        assert connection.execute(text("SELECT count(*) FROM organizations")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM students WHERE organization_id IS NOT NULL")).scalar_one() == 0
    legacy.dispose()


@requires_postgres
def test_a_database_from_before_phase_22_migrates_completely_on_postgres(seeded_source, capsys) -> None:
    with postgres_schema_engine() as legacy:
        counts = build_legacy_database(legacy, seeded_source)
        _assert_migrates(legacy, counts, capsys)
