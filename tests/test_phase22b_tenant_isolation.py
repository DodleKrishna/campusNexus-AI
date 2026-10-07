"""Phase 22B -- TenantContext, tenant-bound sessions and automatic ORM isolation.

Two organizations with overlapping codes: A is the seeded demo institution,
B ("northfield-demo") reuses department code CSE and employee code EMP-CSE-004.
With CAMPUSNEXUS_TEST_ALL_ON_POSTGRES=1 the fixture-based tests run on
PostgreSQL too.
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.api.main import create_app
from app.auth import tokens
from app.auth.accounts import seed_dev_accounts
from app.auth.identity import resolve_identity
from app.auth.passwords import hash_password
from app.auth.tokens import decode_token, issue_token
from app.db.models import (
    AuthAccount, Department, FacultyProfile, Mission, Organization, OrganizationMembership, Student, User,
)
from app.db.tenancy import ensure_default_organization, set_account_role, tenancy_report
from app.db.tenant_session import (
    IDENTITY_LOOKUP, TenantIsolationError, TenantSessionFactory, bind_organization, is_tenant_model,
)
from app.graph.orchestrator import MissionOrchestrator
from app.llm.providers.mock import MockLLMProvider
from app.schemas.enums import MembershipStatus, UserRole
from app.tools.build import build_default_tool_registry

PASSWORD = "test-only-Passw0rd"
A_STUDENT, A_FACULTY, A_ADMIN = "student@campusnexus.local", "faculty@campusnexus.local", "admin@campusnexus.local"
B_STUDENT, B_FACULTY, B_ADMIN = "nina@northfield.test", "omar@northfield.test", "priya@northfield.test"
REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def orgs(seeded_session, session_factory):
    """Organization A (seeded) and B, built through the system path with explicit organizations."""
    with session_factory() as s:
        seed_dev_accounts(s, PASSWORD)
        a = ensure_default_organization(s)
        b = Organization(slug="northfield-demo", name="Northfield Demo College")
        s.add(b)
        s.flush()
        cse = Department(organization_id=b.id, code="CSE", name="Northfield CSE")  # same code as A's CSE
        s.add(cse)
        s.flush()
        user = User(organization_id=b.id, email="nina@northfield.test", full_name="Nina North", role=UserRole.STUDENT)
        s.add(user)
        s.flush()
        student = Student(organization_id=b.id, student_code="NF-2026-001", user_id=user.id, department_id=cse.id,
                          year=3, semester=5, cgpa=8.1, section="1")
        faculty = FacultyProfile(organization_id=b.id, employee_code="EMP-CSE-004", full_name="Dr. Omar North",  # same code as A
                                 department_id=cse.id, designation="Professor", email="omar@northfield.test")
        s.add_all([student, faculty])
        s.flush()
        for email, role, name, links in (
            (B_STUDENT, UserRole.STUDENT, "Nina North", {"student_id": student.id}),
            (B_FACULTY, UserRole.FACULTY, "Dr. Omar North", {"faculty_profile_id": faculty.id}),
            (B_ADMIN, UserRole.ADMIN, "Priya North", {}),
        ):
            account = AuthAccount(email=email, password_hash=hash_password(PASSWORD), role=role, display_name=name,
                                  linked_student_id=student.student_code if role == UserRole.STUDENT else None,
                                  linked_faculty_id=faculty.id if role == UserRole.FACULTY else None)
            s.add(account)
            s.flush()
            s.add(OrganizationMembership(organization_id=b.id, account_id=account.id, role=role, **links))
        s.commit()
        return {"a": a.id, "b": b.id, "b_student": student.id, "b_faculty": faculty.id, "b_cse": cse.id}


@pytest.fixture()
def app(orgs, session_factory, knowledge_service):
    return create_app(session_factory=session_factory, knowledge_service=knowledge_service, llm_provider=MockLLMProvider(),
                      tool_gateway=build_default_tool_registry())


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def tenant_factory(engine) -> TenantSessionFactory:
    return TenantSessionFactory(engine)


def login(client, email: str) -> dict:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def forged(claims: dict) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"iat": now, "exp": now + timedelta(minutes=5), "iss": tokens.ISSUER, **claims},
                      tokens._secret(), algorithm=tokens.ALGORITHM)


def membership_of(session, email: str) -> OrganizationMembership:
    account = session.execute(select(AuthAccount).where(AuthAccount.email == email)).scalar_one()
    return session.execute(select(OrganizationMembership).where(OrganizationMembership.account_id == account.id)).scalar_one()


# --- Authentication ------------------------------------------------------------------------------------------


def test_login_emits_the_organization_and_membership_claims(client, orgs, session_factory) -> None:
    body = login(client, B_STUDENT)
    claims = decode_token(body["access_token"])
    with session_factory() as s:
        membership = membership_of(s, B_STUDENT)
    assert (claims.organization_id, claims.membership_id, claims.role) == (orgs["b"], membership.id, "student")
    assert body["user"]["organization"] == {"id": orgs["b"], "slug": "northfield-demo", "name": "Northfield Demo College"}


def test_me_returns_the_callers_organization(client) -> None:
    for email, slug in ((A_STUDENT, "campusnexus-demo"), (B_FACULTY, "northfield-demo"), (B_ADMIN, "northfield-demo")):
        me = client.get("/auth/me", headers=bearer(login(client, email)["access_token"])).json()
        assert me["organization"]["slug"] == slug and me["email"] == email


def test_a_token_without_tenant_claims_is_rejected(client, session_factory) -> None:
    with session_factory() as s:
        account_id = membership_of(s, A_STUDENT).account_id
    legacy = forged({"sub": str(account_id), "role": "student"})  # a Phase 15 token: validly signed, no org/mid
    assert client.get("/auth/me", headers=bearer(legacy)).status_code == 401


def test_forged_organization_or_membership_claims_are_rejected(client, orgs, session_factory) -> None:
    claims = decode_token(login(client, A_STUDENT)["access_token"])
    with session_factory() as s:
        other_membership = membership_of(s, A_FACULTY).id
        b_membership = membership_of(s, B_STUDENT).id
    for org, mid in ((orgs["b"], claims.membership_id),     # right membership, other organization
                     (claims.organization_id, other_membership),  # right organization, someone else's membership
                     (orgs["b"], b_membership),              # a B membership that is not this account's
                     (999_999, claims.membership_id)):        # no such organization
        token = forged({"sub": str(claims.account_id), "role": "student", "org": org, "mid": mid})
        assert client.get("/auth/me", headers=bearer(token)).status_code == 401, (org, mid)


def test_an_inactive_membership_or_account_is_rejected(client, session_factory) -> None:
    token = login(client, A_STUDENT)["access_token"]
    with session_factory() as s:
        membership_of(s, A_STUDENT).status = MembershipStatus.INACTIVE
        s.commit()
    assert client.get("/auth/me", headers=bearer(token)).status_code == 401
    assert client.post("/auth/login", json={"email": A_STUDENT, "password": PASSWORD}).status_code == 403
    token = login(client, A_FACULTY)["access_token"]
    with session_factory() as s:
        s.execute(select(AuthAccount).where(AuthAccount.email == A_FACULTY)).scalar_one().is_active = False
        s.commit()
    assert client.get("/auth/me", headers=bearer(token)).status_code == 401


def test_the_membership_role_is_authoritative(client, session_factory) -> None:
    with session_factory() as s:  # the compatibility mirror drifts: it must not grant anything
        s.execute(select(AuthAccount).where(AuthAccount.email == A_STUDENT)).scalar_one().role = UserRole.ADMIN
        s.commit()
        assert tenancy_report(s).membership_drift  # and the drift is reported
    body = login(client, A_STUDENT)
    assert body["user"]["role"] == "student" and decode_token(body["access_token"]).role == "student"
    assert client.get("/admin/dashboard", headers=bearer(body["access_token"])).status_code == 403


def test_a_role_change_keeps_membership_and_mirror_in_step(orgs, session_factory) -> None:
    with session_factory() as s:
        account = s.execute(select(AuthAccount).where(AuthAccount.email == A_FACULTY)).scalar_one()
        set_account_role(s, account, UserRole.HOD)
        s.commit()
        assert membership_of(s, A_FACULTY).role == UserRole.HOD and account.role == UserRole.HOD
        assert tenancy_report(s).membership_drift == []


# --- Profiles resolve only through the membership --------------------------------------------------------------


def test_the_student_comes_from_the_membership_not_the_account_link(client, orgs, session_factory) -> None:
    with session_factory() as s:  # a stale legacy link on the account points at an A student
        s.execute(select(AuthAccount).where(AuthAccount.email == B_STUDENT)).scalar_one().linked_student_id = "STU-DEMO-001"
        s.commit()
    token = login(client, B_STUDENT)["access_token"]
    assert client.get("/me/profile", headers=bearer(token)).json()["student_code"] == "NF-2026-001"


def test_the_faculty_profile_comes_from_the_membership(client, orgs, session_factory) -> None:
    with session_factory() as s:
        s.execute(select(AuthAccount).where(AuthAccount.email == B_FACULTY)).scalar_one().linked_faculty_id = None
        s.commit()
    me = client.get("/faculty/me", headers=bearer(login(client, B_FACULTY)["access_token"]))
    assert me.status_code == 200 and me.json()["full_name"] == "Dr. Omar North"


def test_a_profile_in_another_organization_is_rejected(client, orgs, session_factory) -> None:
    with session_factory() as s:  # B's faculty membership wrongly points at A's EMP-CSE-004
        a_faculty = s.execute(select(FacultyProfile).where(FacultyProfile.organization_id == orgs["a"],
                                                           FacultyProfile.employee_code == "EMP-CSE-004")).scalar_one()
        membership_of(s, B_FACULTY).faculty_profile_id = a_faculty.id
        s.commit()
    assert client.post("/auth/login", json={"email": B_FACULTY, "password": PASSWORD}).status_code == 403


# --- Tenant session reads ----------------------------------------------------------------------------------------


def test_each_organization_sees_only_its_own_rows(tenant_factory, orgs) -> None:
    with tenant_factory.open_tenant_session(orgs["a"]) as a, tenant_factory.open_tenant_session(orgs["b"]) as b:
        assert [d.name for d in a.execute(select(Department).where(Department.code == "CSE")).scalars()] == ["Computer Science & Engineering"]
        assert [d.name for d in b.execute(select(Department).where(Department.code == "CSE")).scalars()] == ["Northfield CSE"]
        assert b.execute(select(func.count()).select_from(Student)).scalar_one() == 1
        assert a.execute(select(func.count()).select_from(Student)).scalar_one() > 1
        assert [f.full_name for f in b.execute(select(FacultyProfile).where(FacultyProfile.employee_code == "EMP-CSE-004")).scalars()] == ["Dr. Omar North"]
        assert b.execute(select(func.count()).select_from(OrganizationMembership)).scalar_one() == 3


def test_a_guessed_foreign_id_behaves_as_missing(tenant_factory, orgs) -> None:
    with tenant_factory.open_tenant_session(orgs["a"]) as a:
        assert a.get(Student, orgs["b_student"]) is None
        assert a.get(Department, orgs["b_cse"]) is None
        assert a.execute(select(Student).where(Student.student_code == "NF-2026-001")).first() is None
        assert a.execute(select(Student.id).join(Department).where(Department.id == orgs["b_cse"])).first() is None


def test_relationships_stay_in_the_organization(tenant_factory, orgs) -> None:
    with tenant_factory.open_tenant_session(orgs["b"]) as b:
        student = b.get(Student, orgs["b_student"])
        assert student.department.name == "Northfield CSE"  # lazy load, scoped
        assert [s.student_code for s in b.get(Department, orgs["b_cse"]).students] == ["NF-2026-001"]


def test_a_session_without_an_organization_fails_closed(tenant_factory, orgs) -> None:
    with tenant_factory() as unbound:
        with pytest.raises(TenantIsolationError):
            unbound.execute(select(Student)).all()
        with pytest.raises(TenantIsolationError):
            unbound.get(Department, orgs["b_cse"])
        # A tenant table reached only through a join is still empty (backstop criterion).
        assert unbound.execute(select(AuthAccount.email).join(Student, Student.student_code == AuthAccount.linked_student_id)).all() == []
        unbound.add(Department(code="X", name="Nowhere"))
        with pytest.raises(TenantIsolationError):
            unbound.flush()


def test_a_session_is_bound_once(tenant_factory, orgs) -> None:
    with tenant_factory.open_tenant_session(orgs["a"]) as a:
        bind_organization(a, orgs["a"])  # idempotent
        with pytest.raises(TenantIsolationError):
            bind_organization(a, orgs["b"])


# --- Tenant session writes ---------------------------------------------------------------------------------------


def test_a_new_row_gets_the_session_organization(tenant_factory, orgs) -> None:
    with tenant_factory.open_tenant_session(orgs["b"]) as b:
        ece = Department(code="ECE", name="Northfield ECE")
        b.add(ece)
        b.flush()
        assert ece.organization_id == orgs["b"]
        b.add(Department(code="MECH", name="Northfield Mechanical", organization_id=orgs["b"]))  # explicit, matching
        b.flush()


def test_a_row_for_another_organization_is_rejected(tenant_factory, orgs) -> None:
    with tenant_factory.open_tenant_session(orgs["b"]) as b:
        b.add(Department(code="EVIL", name="Planted", organization_id=orgs["a"]))
        with pytest.raises(TenantIsolationError):
            b.flush()


def test_an_existing_row_cannot_move_between_organizations(tenant_factory, orgs) -> None:
    with tenant_factory.open_tenant_session(orgs["b"]) as b:
        b.get(Department, orgs["b_cse"]).organization_id = orgs["a"]
        with pytest.raises(TenantIsolationError):
            b.flush()


def test_merging_a_foreign_row_is_rejected(tenant_factory, session_factory, orgs) -> None:
    with session_factory() as system:
        a_department = system.execute(select(Department).where(Department.organization_id == orgs["a"])).scalars().first()
        system.expunge(a_department)
    with tenant_factory.open_tenant_session(orgs["b"]) as b:
        b.merge(a_department)  # not visible to B, so it would be inserted -- as an A row
        with pytest.raises(TenantIsolationError):
            b.flush()


# --- Global models and identity lookups --------------------------------------------------------------------------


def test_accounts_and_organizations_are_global(tenant_factory, orgs) -> None:
    assert not is_tenant_model(AuthAccount) and not is_tenant_model(Organization) and is_tenant_model(OrganizationMembership)
    with tenant_factory.open_tenant_session(orgs["b"]) as b:
        assert b.execute(select(func.count()).select_from(AuthAccount)).scalar_one() == 10  # 7 in A + 3 in B, unfiltered
        assert b.execute(select(func.count()).select_from(Organization)).scalar_one() == 2


def test_an_identity_lookup_cannot_reach_tenant_data(tenant_factory, orgs) -> None:
    with tenant_factory() as unbound:
        with pytest.raises(TenantIsolationError):
            unbound.execute(select(Student).execution_options(**{IDENTITY_LOOKUP: True})).all()
        rows = unbound.execute(select(OrganizationMembership).execution_options(**{IDENTITY_LOOKUP: True})).all()
        assert len(rows) == 10  # memberships themselves are identity data


def test_identity_resolution_is_one_statement(tenant_factory, engine, orgs, session_factory) -> None:
    with session_factory() as s:
        membership = membership_of(s, B_FACULTY)
    statements = []
    listener = lambda *args: statements.append(args[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        with tenant_factory() as session:
            identity = resolve_identity(session, account_id=membership.account_id, organization_id=orgs["b"],
                                        membership_id=membership.id)
            assert identity is not None and identity.faculty.full_name == "Dr. Omar North"
            assert identity.context.role == UserRole.FACULTY and identity.context.organization_id == orgs["b"]
            count = len(statements)
            assert session.get(FacultyProfile, identity.faculty.id) is identity.faculty  # identity map, no query
            assert len(statements) == count
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert count == 1, statements


# --- Orchestrator in tenant mode and the demo identity ------------------------------------------------------------


def test_the_orchestrator_needs_an_organization_in_tenant_mode(tenant_factory, orgs) -> None:
    orchestrator = MissionOrchestrator(session_factory=tenant_factory, registry=None, llm_provider=MockLLMProvider())
    with pytest.raises(TenantIsolationError):
        orchestrator.run_mission("Check my attendance", user_id="NF-2026-001", user_role=UserRole.STUDENT)
    with pytest.raises(ValueError):  # another organization's mission is simply unknown
        orchestrator.resume_mission("mission-not-in-b", organization_id=orgs["b"])


def test_api_missions_are_written_into_the_callers_organization(client, orgs, session_factory) -> None:
    response = client.post("/missions", json={"student_id": "STU-DEMO-001", "goal": "Check my Operating Systems attendance"},
                           headers={"X-Demo-Identity": "student-demo"})
    assert response.status_code in (200, 201), response.text
    with session_factory() as s:
        mission = s.get(Mission, response.json()["mission_id"])
        assert mission.organization_id == orgs["a"]
        assert tenancy_report(s).cross_tenant_links == {}


def test_health_works_without_an_organization(client) -> None:
    body = client.get("/health").json()
    assert body["database"]["ready"] is True and body["database"]["students"] == 29  # 28 in A + 1 in B


# --- Guards against bypassing the tenant session ------------------------------------------------------------------

RUNTIME_PACKAGES = ("app/api", "app/services", "app/agents", "app/tools", "app/graph", "app/rag", "app/rules", "app/llm",
                    "app/auth", "app/schemas", "app/agentos")
ALLOWED_TEXT_SQL = {"SELECT 1"}


def _runtime_modules():
    for package in RUNTIME_PACKAGES:
        for path in sorted((REPO / package).rglob("*.py")):
            yield path.relative_to(REPO).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def test_runtime_code_has_no_unscoped_raw_sql() -> None:
    problems = []
    for name, tree in _runtime_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if func_name == "text":
                    literal = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
                    if literal not in ALLOWED_TEXT_SQL:
                        problems.append(f"{name}:{node.lineno} raw text() SQL is not tenant-filtered")
                if func_name == "exec_driver_sql":
                    problems.append(f"{name}:{node.lineno} exec_driver_sql bypasses the tenant session")
            if isinstance(node, ast.Attribute) and node.attr in ("__table__", "tables") and name != "app/api/main.py":
                problems.append(f"{name}:{node.lineno} Core table access ({node.attr}) is not tenant-filtered")
    assert problems == []


def test_runtime_code_never_uses_the_privileged_session_path() -> None:
    problems = []
    for name, tree in _runtime_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.db.system_session":
                problems.append(f"{name}:{node.lineno} imports app.db.system_session")
            if isinstance(node, ast.ImportFrom) and node.module == "app.db.session":
                imported = {alias.name for alias in node.names}
                if imported & {"create_session_factory", "session_scope"}:
                    problems.append(f"{name}:{node.lineno} builds unfiltered sessions ({', '.join(sorted(imported))})")
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "sessionmaker":
                problems.append(f"{name}:{node.lineno} builds a plain sessionmaker")
            if isinstance(node, ast.Name) and node.id == "IDENTITY_LOOKUP" and name != "app/auth/identity.py":
                problems.append(f"{name}:{node.lineno} uses the identity-lookup exemption outside app.auth.identity")
    assert problems == []


def test_tokens_are_issued_only_with_tenant_claims() -> None:
    with pytest.raises(TypeError):
        issue_token(1, "student")  # organization_id / membership_id are required
