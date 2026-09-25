"""Phase 21 -- database portability, offline (SQLite only; no server needed).

Connection selection, the dialect-aware engine factory, secret-free
reporting, the portable column types and the SQLite -> PostgreSQL copy logic
(exercised SQLite -> SQLite here; tests/test_phase21_postgres.py runs it
against a real PostgreSQL server when one is configured).
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.db.base import Base, UTCDateTime
from app.db.models import Department, Event, EventStatus, Student
from app.db.models.workflow import OperationAuditEvent
from app.db.portability import (
    MigrationError,
    compare_counts,
    copy_database,
    deferred_columns,
    find_orphans,
    length_violations,
    table_counts,
)
from app.db.session import (
    BASE_DIR,
    DatabaseConfigError,
    create_database_engine,
    create_db_engine,
    create_session_factory,
    describe_database,
    get_database_url,
    init_db,
    normalize_database_url,
    safe_error,
)
from app.services import admin_ops
from scripts.seed_data import run_seed
from scripts.seed_database import parse_args as seed_args
from scripts.seed_database import reset_refusal

REPO = Path(__file__).resolve().parents[1]
PG_URL = "postgresql://db_user:S3cret-pw@db.example.supabase.co:5432/postgres?sslmode=require"


# --- Connection selection ------------------------------------------------------------------------------


def test_the_suite_never_inherits_a_real_database_url() -> None:
    assert "CAMPUSNEXUS_DATABASE_URL" not in os.environ


def test_database_url_outranks_db_path_which_outranks_the_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CAMPUSNEXUS_DB_PATH", raising=False)
    assert get_database_url() == f"sqlite:///{(BASE_DIR / 'data' / 'campusnexus.db').as_posix()}"
    monkeypatch.setenv("CAMPUSNEXUS_DB_PATH", str(tmp_path / "path.db"))
    assert get_database_url() == f"sqlite:///{(tmp_path / 'path.db').as_posix()}"
    monkeypatch.setenv("CAMPUSNEXUS_DATABASE_URL", PG_URL)
    assert get_database_url().startswith("postgresql+psycopg://db_user:S3cret-pw@db.example.supabase.co:5432/postgres")
    # An explicit path (tests, evals, the local demo reset) is always that SQLite file.
    assert get_database_url(db_path=tmp_path / "explicit.db") == f"sqlite:///{(tmp_path / 'explicit.db').as_posix()}"


def test_database_url_may_also_name_sqlite(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_DATABASE_URL", f"sqlite:///{(tmp_path / 'url.db').as_posix()}")
    assert get_database_url() == f"sqlite:///{(tmp_path / 'url.db').as_posix()}"


@pytest.mark.parametrize("raw", ["postgres://u:p@h/db", "postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"])
def test_postgres_urls_use_the_psycopg3_driver(raw: str) -> None:
    assert normalize_database_url(raw) == "postgresql+psycopg://u:p@h/db"


@pytest.mark.parametrize("raw", ["postgresql+psycopg2://u:S3cret@h/db", "mysql://u:S3cret@h/db", "not a url S3cret"])
def test_unsupported_urls_are_refused_without_echoing_them(raw: str) -> None:
    with pytest.raises(DatabaseConfigError) as error:
        normalize_database_url(raw)
    assert "S3cret" not in str(error.value)


# --- Engine factory ------------------------------------------------------------------------------------


def test_sqlite_engine_keeps_foreign_keys_and_thread_sharing(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'fk.db').as_posix()}")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    engine.dispose()


def test_postgres_engine_is_pooled_pre_pinged_and_never_connects_on_creation() -> None:
    pytest.importorskip("psycopg")
    engine = create_database_engine(normalize_database_url("postgresql://u:p@127.0.0.1:1/db"))  # nothing listens there
    assert engine.dialect.name == "postgresql" and engine.dialect.driver == "psycopg"
    assert engine.pool._pre_ping is True
    assert engine.pool.size() == 5 and engine.pool._max_overflow == 5 and engine.pool._recycle == 1800
    assert engine.pool.checkedout() == 0
    engine.dispose()


def test_importing_the_api_never_connects_to_the_configured_database() -> None:
    pytest.importorskip("psycopg")
    env = {**os.environ, "CAMPUSNEXUS_DATABASE_URL": "postgresql://u:p@127.0.0.1:1/unreachable",
           "CAMPUSNEXUS_EMBEDDING_PROVIDER": "deterministic"}
    result = subprocess.run([sys.executable, "-c", "import app.api.main"], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]


# --- Secret-free reporting -----------------------------------------------------------------------------


def test_database_description_never_contains_connection_details() -> None:
    described = describe_database(normalize_database_url(PG_URL))
    assert described == {"dialect": "postgresql", "label": "PostgreSQL / Supabase", "supabase": True}
    assert describe_database("postgresql+psycopg://u:p@localhost/db")["label"] == "PostgreSQL"
    assert describe_database("sqlite:///x.db")["label"] == "SQLite"


def test_safe_error_removes_url_parts_and_addresses() -> None:
    error = RuntimeError('connection to server at "db.example.supabase.co" (10.1.2.3), port 5432 failed: '
                         'password authentication failed for user "db_user" (S3cret-pw)')
    message = safe_error(error, normalize_database_url(PG_URL))
    for secret in ("db.example.supabase.co", "10.1.2.3", "db_user", "S3cret-pw"):
        assert secret not in message
    assert message.startswith("RuntimeError: connection to server at")


def test_health_reports_the_dialect(api_client) -> None:
    database = api_client.get("/health").json()["database"]
    assert database["ready"] is True and database["dialect"] == "sqlite" and database["label"] == "SQLite"


def test_admin_settings_name_the_database_without_its_url(seeded_session, knowledge_service) -> None:
    from app.llm.providers.mock import MockLLMProvider

    status = admin_ops.system_status(seeded_session, MockLLMProvider(), knowledge_service)
    assert status.settings["database_mode"].startswith("SQLite")
    assert "sqlite:///" not in str(status.model_dump())


# --- Types ---------------------------------------------------------------------------------------------


def test_utc_datetime_is_timestamptz_on_postgres_and_aware_everywhere() -> None:
    column = UTCDateTime()
    pg, lite = postgresql.dialect(), sqlite.dialect()
    ist = timezone(timedelta(hours=5, minutes=30))
    value = datetime(2026, 9, 30, 10, 0, tzinfo=ist)
    assert column.dialect_impl(pg).timezone is True
    assert column.process_bind_param(value, pg) == datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc)
    assert column.process_bind_param(value, lite) == datetime(2026, 9, 30, 4, 30)
    assert column.process_result_value(datetime(2026, 9, 30, 0, 30, tzinfo=timezone(timedelta(hours=-4))), pg) == \
        datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc)
    assert column.process_result_value(datetime(2026, 9, 30, 4, 30), lite).tzinfo == timezone.utc
    for dialect in (pg, lite):
        with pytest.raises(ValueError, match="timezone-aware"):
            column.process_bind_param(datetime(2026, 9, 30, 10, 0), dialect)


def test_enums_are_portable_varchars_with_no_native_postgres_type() -> None:
    enums = [c for t in Base.metadata.sorted_tables for c in t.columns if type(c.type).__name__ == "Enum"]
    assert enums and all(not c.type.native_enum and c.type.length == 64 for c in enums)
    ddl = "\n".join(str(CreateTable(t).compile(dialect=postgresql.dialect())) for t in Base.metadata.sorted_tables)
    assert "CREATE TYPE" not in ddl and " ENUM" not in ddl.upper()


def test_audit_action_filter_is_case_insensitive_on_every_dialect(seeded_session) -> None:
    seeded_session.add(OperationAuditEvent(event_type="request_approved", actor_role="hod", subject_type="workflow_request",
                                           subject_id="1", message="ok"))
    seeded_session.commit()
    entries = admin_ops.audit_log(seeded_session, source="operations", action="APPROVED")
    assert [e.action for e in entries] == ["request_approved"]


# --- Copying (SQLite -> SQLite; the same code runs SQLite -> PostgreSQL) ---------------------------------


def _seeded(path: Path):
    engine = create_db_engine(db_path=path)
    init_db(engine)
    with create_session_factory(engine)() as session:
        run_seed(session)
        session.add(OperationAuditEvent(event_type="copied", actor_role="system", subject_type="test", subject_id="1",
                                        message="json", event_metadata={"k": [1, 2]}))
        session.commit()
    return engine


def _empty(path: Path):
    engine = create_db_engine(db_path=path)
    init_db(engine)
    return engine


def test_copy_preserves_ids_json_timestamps_and_the_hod_cycle(tmp_path) -> None:
    assert deferred_columns(Department.__table__) == ["hod_faculty_id"]
    source, target = _seeded(tmp_path / "s.db"), _empty(tmp_path / "t.db")
    report = copy_database(source, target)
    checks = compare_counts(source, target)
    assert all(c.ok for c in checks) and report.inserted == sum(c.source for c in checks) > 0
    assert find_orphans(target) == []
    with create_session_factory(source)() as a, create_session_factory(target)() as b:
        for model in (Student, Department, Event, OperationAuditEvent):
            rows_a = [{c.name: getattr(r, c.key) for c in model.__table__.columns} for r in a.execute(select(model).order_by(model.id)).scalars()]
            rows_b = [{c.name: getattr(r, c.key) for c in model.__table__.columns} for r in b.execute(select(model).order_by(model.id)).scalars()]
            assert rows_a == rows_b
        assert b.execute(select(Department).where(Department.code == "CSE")).scalar_one().hod_faculty_id is not None
    source.dispose(), target.dispose()


def test_copy_refuses_a_populated_target_and_merge_identical_is_a_no_op(tmp_path) -> None:
    source, target = _seeded(tmp_path / "s.db"), _empty(tmp_path / "t.db")
    copy_database(source, target)
    before = table_counts(target)
    with pytest.raises(MigrationError, match="already holds data"):
        copy_database(source, target)
    assert copy_database(source, target, merge_identical=True).inserted == 0
    assert table_counts(target) == before
    with target.begin() as connection:
        connection.execute(text("UPDATE departments SET name = 'Changed' WHERE code = 'CSE'"))
    with pytest.raises(MigrationError, match="differs from the source"):
        copy_database(source, target, merge_identical=True)
    source.dispose(), target.dispose()


def test_dry_run_writes_nothing(tmp_path) -> None:
    source, target = _seeded(tmp_path / "s.db"), _empty(tmp_path / "t.db")
    report = copy_database(source, target, dry_run=True)
    assert report.dry_run and report.inserted > 0
    assert sum(table_counts(target).values()) == 0
    source.dispose(), target.dispose()


def test_values_postgres_would_reject_and_orphans_stop_the_copy_before_writing(tmp_path) -> None:
    source, target = _seeded(tmp_path / "s.db"), _empty(tmp_path / "t.db")
    with source.begin() as connection:
        connection.execute(text("UPDATE departments SET code = :v WHERE code = 'CSE'"), {"v": "X" * 30})
    assert length_violations(source) == ["departments.code (max 30 > 20)"]
    with pytest.raises(MigrationError, match="longer than their column allows"):
        copy_database(source, target)
    assert sum(table_counts(target).values()) == 0
    orphaned = _seeded(tmp_path / "o.db")
    with orphaned.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(text("UPDATE enrollments SET student_id = 99999 WHERE id = 1"))
        connection.commit()
    assert [(o.table, o.parent) for o in find_orphans(orphaned)] == [("enrollments", "students")]
    with pytest.raises(MigrationError, match="orphan rows"):
        copy_database(orphaned, target)
    for engine in (source, target, orphaned):
        engine.dispose()


def test_migration_script_requires_a_postgres_target(monkeypatch, tmp_path, capsys) -> None:
    from scripts.migrate_sqlite_to_postgres import main

    _seeded(tmp_path / "s.db").dispose()
    monkeypatch.setenv("CAMPUSNEXUS_DATABASE_URL", f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    assert main(["--source", str(tmp_path / "s.db"), "--dry-run"]) == 2
    assert "must name a PostgreSQL database" in capsys.readouterr().out


# --- Remote reset guard ---------------------------------------------------------------------------------


def test_remote_reset_needs_the_flag_and_the_database_name() -> None:
    url = normalize_database_url(PG_URL)
    assert "--allow-remote-reset" in reset_refusal(url, seed_args(["--reset"]))
    assert "--confirm-database postgres" in reset_refusal(url, seed_args(["--reset", "--allow-remote-reset"]))
    assert reset_refusal(url, seed_args(["--reset", "--allow-remote-reset", "--confirm-database", "other"]))
    assert reset_refusal(url, seed_args(["--reset", "--allow-remote-reset", "--confirm-database", "postgres"])) is None
    assert reset_refusal("sqlite:///local.db", seed_args(["--reset"])) is None


def test_seed_database_seeds_sqlite_idempotently(tmp_path, capsys, monkeypatch) -> None:
    from scripts import seed_database

    monkeypatch.setenv("CAMPUSNEXUS_DEMO_PASSWORD", "test-only-Passw0rd")
    monkeypatch.setattr(seed_database, "SQLITE_CREDENTIALS", tmp_path / "creds.txt")
    db = tmp_path / "seeded.db"
    assert seed_database.main(["--sqlite", str(db)]) == 0
    engine = create_db_engine(db_path=db)
    first = table_counts(engine)
    assert seed_database.main(["--sqlite", str(db)]) == 0
    assert table_counts(engine) == first and first["students"] > 0
    with create_session_factory(engine)() as session:
        assert session.execute(select(Student).where(Student.student_code == "STU-DEMO-001")).scalar_one().user.full_name == "Aditi Rao"
    engine.dispose()
    assert "test-only-Passw0rd" not in capsys.readouterr().out


def test_seed_database_adds_nothing_when_run_again_on_a_later_day(tmp_path, monkeypatch) -> None:
    """seed_data.run_seed dates are relative to "now"; a later-day run must not add a second copy of anything."""
    from scripts import seed_data, seed_database

    monkeypatch.setenv("CAMPUSNEXUS_DEMO_PASSWORD", "test-only-Passw0rd")
    monkeypatch.setattr(seed_database, "SQLITE_CREDENTIALS", tmp_path / "creds.txt")
    db = tmp_path / "seeded.db"
    assert seed_database.main(["--sqlite", str(db)]) == 0
    engine = create_db_engine(db_path=db)
    first = table_counts(engine)
    monkeypatch.setattr(seed_data, "SEED_NOW", seed_data.SEED_NOW + timedelta(days=3))
    assert seed_database.main(["--sqlite", str(db)]) == 0
    assert table_counts(engine) == first
    engine.dispose()


def test_event_status_values_survive_as_plain_strings(tmp_path) -> None:
    engine = _seeded(tmp_path / "s.db")
    with engine.connect() as connection:
        raw = set(connection.execute(text("SELECT DISTINCT status FROM events")).scalars())
    assert raw <= {s.value for s in EventStatus}
    engine.dispose()
