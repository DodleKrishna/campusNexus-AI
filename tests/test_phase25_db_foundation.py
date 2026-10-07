"""AgentOS V2 Phase 2.5 -- production data foundation (offline; PostgreSQL parts skip without a server).

Database modes (local SQLite / postgres with no fallback), the explicit psycopg and pg8000 drivers, bounded and
SSL-safe PostgreSQL engines, secret-free errors/health/preflight, PostgreSQL DDL compilation of every model, the
transactional additive upgrade (idempotent, from a Phase-21-era database) and the AgentOS/tenant indexes.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import pg8000 as pg8000_dialect
from sqlalchemy.dialects.postgresql import psycopg as psycopg_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db.base import Base
from app.db.models import AgentMission, AgentStep, AuthAccount, DomainEvent
from app.db.models.agent_kernel import AgentMissionStatus, AgentStepStatus
from app.db.preflight import AGENTOS_INDEXES, AGENTOS_TABLES, CORE_TABLES, run_preflight, safe_host
from app.db.session import (
    DatabaseConfigError,
    DatabaseUnavailableError,
    create_database_engine,
    create_db_engine,
    create_session_factory,
    database_mode,
    get_database_url,
    init_db,
    normalize_database_url,
    plan_schema_upgrade,
    upgrade_schema,
    verify_database_ready,
)
from app.db.tenancy import ensure_default_organization
from app.schemas.enums import UserRole
from scripts.seed_data import run_seed
from tests.pg_support import postgres_schema_engine, requires_postgres
from tests.test_phase22a_tenancy import build_legacy_database

REPO = Path(__file__).resolve().parents[1]
SECRET_PASSWORD, SECRET_USER = "S3cret-pw-25", "postgres.projref25abc"
# Nothing listens on port 1, so connecting fails fast and offline.
UNREACHABLE = f"postgresql+pg8000://{SECRET_USER}:{SECRET_PASSWORD}@127.0.0.1:1/postgres"
SECRETS = (SECRET_PASSWORD, SECRET_USER, "projref25abc", UNREACHABLE)
# Tables a Phase-21-era database did not have yet (tenancy, AI usage/deployments, AgentOS kernel).
PHASE21_MISSING = ("organizations", "organization_memberships", "ai_usage_events", "agent_deployments",
                   *AGENTOS_TABLES)


def _no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    for name in ("CAMPUSNEXUS_DATABASE_MODE", "CAMPUSNEXUS_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CAMPUSNEXUS_DB_PATH", str(tmp_path / "local.db"))
    return monkeypatch


# --- 1-3. Modes ------------------------------------------------------------------------------------------------


def test_local_mode_uses_sqlite(clean_env, tmp_path) -> None:
    assert database_mode() == "local"
    clean_env.setenv("CAMPUSNEXUS_DATABASE_MODE", "local")
    assert get_database_url() == f"sqlite:///{(tmp_path / 'local.db').as_posix()}"
    assert create_db_engine().dialect.name == "sqlite"


def test_local_mode_refuses_a_postgres_url_instead_of_choosing(clean_env) -> None:
    clean_env.setenv("CAMPUSNEXUS_DATABASE_MODE", "local")
    clean_env.setenv("CAMPUSNEXUS_DATABASE_URL", UNREACHABLE)
    with pytest.raises(DatabaseConfigError, match="local uses SQLite") as error:
        get_database_url()
    _no_secrets(str(error.value))


def test_postgres_mode_requires_a_postgres_database_url(clean_env, tmp_path) -> None:
    clean_env.setenv("CAMPUSNEXUS_DATABASE_MODE", "postgres")
    with pytest.raises(DatabaseConfigError, match="requires CAMPUSNEXUS_DATABASE_URL"):
        get_database_url()  # CAMPUSNEXUS_DB_PATH is set, and still not used
    clean_env.setenv("CAMPUSNEXUS_DATABASE_URL", f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    with pytest.raises(DatabaseConfigError, match="requires a PostgreSQL"):
        get_database_url()
    clean_env.setenv("CAMPUSNEXUS_DATABASE_URL", UNREACHABLE)
    assert get_database_url() == UNREACHABLE


def test_an_unknown_mode_is_refused(clean_env) -> None:
    clean_env.setenv("CAMPUSNEXUS_DATABASE_MODE", "cloud")
    with pytest.raises(DatabaseConfigError, match="local, postgres"):
        get_database_url()


def test_unset_mode_keeps_the_phase21_inference(clean_env) -> None:
    clean_env.setenv("CAMPUSNEXUS_DATABASE_URL", UNREACHABLE)
    assert database_mode() == "postgres" and get_database_url() == UNREACHABLE


def test_postgres_mode_never_falls_back_when_the_server_is_unreachable(caplog) -> None:
    engine = create_database_engine(UNREACHABLE)
    with caplog.at_level(logging.DEBUG), pytest.raises(DatabaseUnavailableError, match="database unreachable") as error:
        verify_database_ready(engine)
    _no_secrets(str(error.value))
    _no_secrets(caplog.text)
    engine.dispose()


def test_api_startup_fails_visibly_in_postgres_mode(tmp_path) -> None:
    """The real app module: no URL -> refuses at import; unreachable -> startup fails. Never SQLite instead."""
    base = {**os.environ, "CAMPUSNEXUS_EMBEDDING_PROVIDER": "deterministic", "CAMPUSNEXUS_DATABASE_MODE": "postgres",
            "CAMPUSNEXUS_DB_PATH": str(tmp_path / "must_not_exist.db"), "CAMPUSNEXUS_VECTOR_STORE_PATH": str(tmp_path / "chroma")}
    base.pop("CAMPUSNEXUS_DATABASE_URL", None)
    missing = subprocess.run([sys.executable, "-c", "import app.api.main"], cwd=REPO, env=base,
                             capture_output=True, text=True, timeout=300)
    assert missing.returncode != 0 and "requires CAMPUSNEXUS_DATABASE_URL" in missing.stderr
    startup = subprocess.run(
        [sys.executable, "-c", "from fastapi.testclient import TestClient; import app.api.main as m; TestClient(m.app).__enter__()"],
        cwd=REPO, env={**base, "CAMPUSNEXUS_DATABASE_URL": UNREACHABLE}, capture_output=True, text=True, timeout=300)
    assert startup.returncode != 0 and "DatabaseUnavailableError" in startup.stderr
    _no_secrets(startup.stdout + startup.stderr)
    assert not (tmp_path / "must_not_exist.db").exists()


# --- Drivers and engine safety -----------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("postgresql+pg8000://u:p@h/db", "postgresql+pg8000://u:p@h/db"),
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("postgres+pg8000://u:p@h/db", "postgresql+pg8000://u:p@h/db"),
    ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),  # no driver named: the documented default
])
def test_the_url_driver_is_kept_as_written(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected


def test_pg8000_engine_is_bounded_pre_pinged_and_lazy(monkeypatch) -> None:
    pytest.importorskip("pg8000")
    engine = create_database_engine(UNREACHABLE)
    assert engine.dialect.driver == "pg8000" and engine.pool._pre_ping is True
    assert engine.pool.size() == 5 and engine.pool._max_overflow == 5 and engine.pool._timeout == 30
    assert engine.pool.checkedout() == 0
    engine.dispose()
    monkeypatch.setenv("CAMPUSNEXUS_DB_MAX_OVERFLOW", "-1")  # would make the pool unbounded
    with pytest.raises(DatabaseConfigError, match="from 0 to 20"):
        create_database_engine(UNREACHABLE)
    monkeypatch.setenv("CAMPUSNEXUS_DB_MAX_OVERFLOW", "500")
    with pytest.raises(DatabaseConfigError, match="from 0 to 20"):
        create_database_engine(UNREACHABLE)


def _captured_args(engine) -> dict:
    """The kwargs the pool passes to the DBAPI connect (cparams), read without connecting."""
    from sqlalchemy import event

    captured: dict = {}

    def capture(_dialect, _conn_rec, cargs, cparams):
        captured.update(cparams)
        raise _Stop()

    event.listen(engine, "do_connect", capture)
    with pytest.raises(_Stop):
        engine.connect()
    return captured


class _Stop(Exception):
    pass


def test_supabase_hosts_always_use_ssl() -> None:
    pytest.importorskip("pg8000")
    pooler = "postgresql+pg8000://postgres.ref:pw@aws-0-region.pooler.supabase.com:5432/postgres"
    args = _captured_args(create_database_engine(pooler))
    assert isinstance(args["ssl_context"], ssl.SSLContext) and "sslmode" not in args
    assert args["timeout"] == 60 and args["application_name"] == "campusnexus"
    for weak in ("disable", "prefer"):
        with pytest.raises(DatabaseConfigError, match="require SSL"):
            create_database_engine(f"{pooler}?sslmode={weak}")
    full = _captured_args(create_database_engine(f"{pooler}?sslmode=verify-full"))["ssl_context"]
    assert full.check_hostname is True and full.verify_mode == ssl.CERT_REQUIRED
    local = _captured_args(create_database_engine("postgresql+pg8000://u:p@127.0.0.1:1/db"))
    assert "ssl_context" not in local  # a non-Supabase host follows its URL


def test_psycopg_gets_sslmode_and_finite_timeouts() -> None:
    try:
        engine = create_database_engine("postgresql+psycopg://u:p@db.ref.supabase.co:5432/postgres")
    except ImportError:
        pytest.skip("psycopg cannot load on this machine (native libpq blocked)")
    args = _captured_args(engine)
    assert args["sslmode"] == "require" and args["connect_timeout"] == 15 and args["prepare_threshold"] is None


# --- 6. PostgreSQL DDL for every current model --------------------------------------------------------------------


@pytest.mark.parametrize("dialect", [postgresql.dialect(), pg8000_dialect.dialect(), psycopg_dialect.dialect()],
                         ids=["postgresql", "pg8000", "psycopg"])
def test_every_model_compiles_for_postgres(dialect) -> None:
    ddl = []
    for table in Base.metadata.sorted_tables:
        ddl.append(str(CreateTable(table).compile(dialect=dialect)))
        ddl.extend(str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes)
    rendered = "\n".join(ddl)
    assert "CREATE TYPE" not in rendered and "JSONB" not in rendered
    assert "TIMESTAMP WITH TIME ZONE" in rendered
    assert "CREATE UNIQUE INDEX ux_organization_memberships_active_account" in rendered and "WHERE status = 'active'" in rendered


def test_tenant_tables_and_agentos_columns_are_postgres_safe() -> None:
    tenant_exempt = {"organizations", "auth_accounts"}
    for table in Base.metadata.sorted_tables:
        if table.name not in tenant_exempt:
            assert "organization_id" in table.columns, table.name
            assert any(list(i.columns)[0].name == "organization_id" for i in table.indexes) or any(
                list(c.columns)[0].name == "organization_id" for c in table.constraints if hasattr(c, "columns") and c.columns
                and type(c).__name__ == "UniqueConstraint"), table.name
    for model in (AgentMission, AgentStep, DomainEvent):
        for column in model.__table__.columns:
            if type(column.type).__name__ == "UTCDateTime":
                assert column.type.dialect_impl(postgresql.dialect()).timezone is True


# --- 9-10. Indexes ---------------------------------------------------------------------------------------------


def test_agentos_workload_indexes_exist(engine) -> None:
    inspector = inspect(engine)
    indexes = {i["name"]: i for name in AGENTOS_TABLES for i in inspector.get_indexes(name)}
    assert set(AGENTOS_INDEXES) <= set(indexes)
    assert indexes["ix_agent_missions_org_owner_agent"]["column_names"] == ["organization_id", "owner_account_id", "agent_key"]
    assert indexes["ix_agent_missions_org_status_wake"]["column_names"] == ["organization_id", "status", "next_wake_at"]
    assert indexes["ix_domain_events_org_type_consumed"]["column_names"] == ["organization_id", "event_type", "consumed_at"]


def test_every_tenant_table_has_an_organization_index(engine) -> None:
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if "organization_id" not in table.columns:
            continue
        leading = {i["column_names"][0] for i in inspector.get_indexes(table.name)}
        leading |= {u["column_names"][0] for u in inspector.get_unique_constraints(table.name)}
        assert "organization_id" in leading, table.name


# --- 5. SQLite AgentOS models ----------------------------------------------------------------------------------


def test_agentos_models_work_on_sqlite(session) -> None:
    org = ensure_default_organization(session)
    account = AuthAccount(email="kernel25@campusnexus.local", password_hash="x", role=UserRole.STUDENT, display_name="K")
    session.add(account)
    session.flush()
    now = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
    mission = AgentMission(organization_id=org.id, owner_account_id=account.id, created_by_account_id=account.id,
                           agent_key="nexus", goal="g", status=AgentMissionStatus.WAITING_EVENT, next_wake_at=now,
                           context={"k": [1]})
    session.add(mission)
    session.flush()
    session.add_all([
        AgentStep(organization_id=org.id, mission_id=mission.id, step_number=1, agent_key="nexus", action_type="wait",
                  status=AgentStepStatus.EXECUTED, output_summary={"ok": True}),
        DomainEvent(organization_id=org.id, event_type="approval_decided", subject_type="agent_mission",
                    subject_id=str(mission.id), payload={"a": 1}),
    ])
    session.commit()
    due = session.execute(select(AgentMission).where(
        AgentMission.organization_id == org.id, AgentMission.status == AgentMissionStatus.WAITING_EVENT,
        AgentMission.next_wake_at <= now)).scalar_one()
    assert due.next_wake_at == now and due.context == {"k": [1]}
    assert session.execute(select(func.count()).select_from(DomainEvent).where(
        DomainEvent.organization_id == org.id, DomainEvent.event_type == "approval_decided",
        DomainEvent.consumed_at.is_(None))).scalar_one() == 1


# --- 8. Upgrade -----------------------------------------------------------------------------------------------


def test_a_current_database_needs_no_upgrade_and_upgrade_is_idempotent(engine) -> None:
    assert plan_schema_upgrade(engine).current
    assert upgrade_schema(engine) == [] and upgrade_schema(engine) == []
    assert verify_database_ready(engine).current


@pytest.fixture()
def seeded_source(tmp_path):
    source = create_db_engine(db_path=str(tmp_path / "current.db"))
    init_db(source)
    with create_session_factory(source)() as session:
        run_seed(session)
    yield source
    source.dispose()


def _assert_upgrades_from_phase21(legacy, counts) -> None:
    before = plan_schema_upgrade(legacy)
    assert not before.current and not before.blocked_columns
    assert set(PHASE21_MISSING) <= {t.name for t in before.missing_tables}
    with pytest.raises(DatabaseUnavailableError, match="upgrade_database.py"):
        verify_database_ready(legacy)  # the API refuses to start on it, and changes nothing
    assert plan_schema_upgrade(legacy).changes == before.changes
    applied = upgrade_schema(legacy)
    assert "students.organization_id" in applied and "index:ix_students_organization_id" in applied
    assert set(PHASE21_MISSING) <= set(applied)
    assert upgrade_schema(legacy) == [] and plan_schema_upgrade(legacy).current  # idempotent
    with legacy.connect() as connection:  # nothing dropped, nothing lost
        for name, expected in counts.items():
            assert connection.execute(select(func.count()).select_from(Base.metadata.tables[name])).scalar_one() == expected, name
    report = run_preflight(engine=legacy)
    assert report.ok, report.lines()


def test_a_phase21_era_sqlite_database_upgrades_additively(tmp_path, seeded_source) -> None:
    legacy = create_db_engine(db_path=str(tmp_path / "phase21.db"))
    counts = build_legacy_database(legacy, seeded_source, exclude=PHASE21_MISSING)
    assert counts["students"] > 0
    _assert_upgrades_from_phase21(legacy, counts)
    legacy.dispose()


@requires_postgres
def test_a_phase21_era_postgres_database_upgrades_additively(seeded_source) -> None:
    with postgres_schema_engine() as legacy:
        counts = build_legacy_database(legacy, seeded_source, exclude=PHASE21_MISSING)
        _assert_upgrades_from_phase21(legacy, counts)


# --- 7. Preflight ---------------------------------------------------------------------------------------------


def test_preflight_passes_on_a_current_sqlite_database(engine) -> None:
    report = run_preflight(engine.url.render_as_string(hide_password=False))
    assert report.ok, report.lines()
    names = {c.name for c in report.checks}
    assert {"driver", "connectivity", "core tables", "AgentOS tables", "organization_id columns", "tenant indexes",
            "AgentOS indexes", "schema upgrade state"} <= names
    assert report.mode == "local" and report.dialect == "sqlite" and report.host.startswith("local file ")
    assert str(engine.url.database) not in json.dumps(report.as_dict())  # the file name only, never the path


def test_preflight_fails_and_redacts_an_unreachable_postgres() -> None:
    pytest.importorskip("pg8000")
    report = run_preflight(UNREACHABLE)
    assert not report.ok and report.mode == "postgres" and report.driver == "pg8000"
    assert [c.name for c in report.checks if not c.ok] == ["connectivity"]
    _no_secrets(json.dumps(report.as_dict()) + "\n".join(report.lines()))


def test_preflight_reports_missing_tables_and_indexes(tmp_path) -> None:
    engine = create_db_engine(db_path=str(tmp_path / "partial.db"))
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[n] for n in CORE_TABLES])
    report = run_preflight(engine.url.render_as_string(hide_password=False))
    failed = {c.name: c.detail for c in report.checks if not c.ok}
    assert "agent_missions" in failed["AgentOS tables"] and "AgentOS indexes" in failed
    assert "pending" in failed["schema upgrade state"]
    engine.dispose()


@pytest.mark.parametrize("url,expected", [
    ("postgresql+pg8000://postgres.abcref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres", "Supabase session pooler (port 5432)"),
    ("postgresql+pg8000://postgres.abcref:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres", "Supabase transaction pooler (port 6543)"),
    ("postgresql+psycopg://postgres:pw@db.abcref.supabase.co:5432/postgres", "Supabase direct connection (port 5432)"),
    ("postgresql+psycopg://u:pw@10.0.0.5:5433/app", "remote host (redacted), port 5433"),
    ("postgresql+psycopg://u:pw@localhost/app", "localhost:5432"),
])
def test_safe_host_never_names_the_host_or_project(url: str, expected: str) -> None:
    assert safe_host(url) == expected
    assert "abcref" not in expected and "ap-south-1" not in expected and "10.0.0.5" not in expected


def test_preflight_script_exit_codes(clean_env, capsys, engine) -> None:
    from scripts.database_preflight import main

    clean_env.setenv("CAMPUSNEXUS_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    assert main([]) == 0
    clean_env.setenv("CAMPUSNEXUS_DATABASE_MODE", "postgres")
    assert main([]) == 2  # a SQLite URL under postgres mode: configuration error, nothing connected
    clean_env.setenv("CAMPUSNEXUS_DATABASE_URL", UNREACHABLE)
    assert main(["--json"]) == 1
    _no_secrets(capsys.readouterr().out)


def test_upgrade_script_dry_run_changes_nothing(clean_env, tmp_path, capsys) -> None:
    from scripts.upgrade_database import main

    db = tmp_path / "old.db"
    engine = create_db_engine(db_path=str(db))
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[n] for n in CORE_TABLES])
    clean_env.setenv("CAMPUSNEXUS_DATABASE_URL", f"sqlite:///{db.as_posix()}")
    assert main(["--dry-run"]) == 0
    assert "agent_missions" not in inspect(engine).get_table_names()
    assert main([]) == 0
    assert "agent_missions" in inspect(engine).get_table_names() and plan_schema_upgrade(engine).current
    assert "Result: all required checks passed" in capsys.readouterr().out
    engine.dispose()


# --- 11. Health ------------------------------------------------------------------------------------------------


def test_health_reports_status_and_type_only(api_client, engine) -> None:
    body = api_client.get("/health").json()["database"]
    assert body["status"] == "connected" and body["type"] == "sqlite"
    rendered = json.dumps(body)
    assert "sqlite:///" not in rendered and Path(str(engine.url.database)).name not in rendered


def test_health_reports_an_unreachable_postgres_without_details(knowledge_service) -> None:
    pytest.importorskip("pg8000")
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.api.main import create_app
    from app.llm.providers.mock import MockLLMProvider

    engine = create_database_engine(UNREACHABLE)
    app = create_app(session_factory=sessionmaker(bind=engine), knowledge_service=knowledge_service,
                     llm_provider=MockLLMProvider())
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    database = response.json()["database"]
    assert database["status"] == "unavailable" and database["type"] == "postgresql" and database["ready"] is False
    _no_secrets(response.text)
    assert "127.0.0.1" not in response.text and "pg8000" not in response.text
    engine.dispose()
