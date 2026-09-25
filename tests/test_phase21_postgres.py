"""Phase 21 -- optional PostgreSQL integration layer.

Skipped unless ``CAMPUSNEXUS_TEST_DATABASE_URL`` names a PostgreSQL database
(never needed for the normal offline suite). Every test runs in its own
throwaway schema (tests/pg_support.py), so a shared or cloud database is never
polluted and ``public`` is never touched.

Besides the PostgreSQL-specific checks below, this module re-runs real
end-to-end flows from earlier phases (profile, attendance, workflow requests,
approvals, missions, audit, candidate selection) on PostgreSQL: the imported
tests pick up this module's ``engine`` fixture, which overrides the SQLite one.

Test strategy: the whole module is the regression suite for a local
PostgreSQL. Against remote Supabase, where every test builds and seeds its
own schema over a high-latency link, only the representative subset in
``SUPABASE_ACCEPTANCE`` runs (``-m supabase_acceptance``; the marker is added by
tests/conftest.py), each test still in its own throwaway schema.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, inspect, select, text

from app.db.models import Department, Event, EventStatus, Mission, Student
from app.db.models.workflow import OperationAuditEvent
from app.db.portability import compare_counts, copy_database, find_orphans, missing_foreign_keys
from app.db.session import create_database_engine, create_db_engine, create_session_factory, init_db, upgrade_schema
from scripts.seed_data import build_summary, run_seed
from tests.pg_support import postgres_schema_engine, postgres_test_url, requires_postgres

pytestmark = requires_postgres

# One representative test per cloud-relevant area (run with -m supabase_acceptance).
SUPABASE_ACCEPTANCE = (
    "test_dashboard_uses_real_records_and_deterministic_rules",              # auth + profile/dashboard persistence
    "test_session_lifecycle_start_mark_close_updates_counters_once",         # attendance read/write
    "test_event_permission_preview_confirm_approve_and_notify",              # workflow request + notification
    "test_full_approve_and_resume_persists_real_row",                        # approval persistence
    "test_full_run_persists_mission_step_agent_run_and_audit_trail",         # mission/agent-run persistence
    "test_audit_log_merges_mission_and_operations_sources_read_only",        # audit persistence
    "test_selecting_a_safe_event_continues_the_same_mission_to_one_approval",  # candidate selection
    "test_every_connection_stays_in_its_throwaway_schema",                   # schema isolation on the pooler
)


@pytest.fixture()
def engine(tmp_path, monkeypatch, request):
    monkeypatch.setenv("CAMPUSNEXUS_DB_PATH", str(tmp_path / "unused.db"))
    with postgres_schema_engine() as eng:
        init_db(eng)
        yield eng
        leaked = eng.pool.checkedout()
        if leaked:
            import warnings
            warnings.warn(f"{request.node.name}: {leaked} connection(s) still checked out at teardown")


# --- Real flows from earlier phases, re-run on PostgreSQL ------------------------------------------------
from tests.test_api_approvals import test_full_approve_and_resume_persists_real_row  # noqa: E402,F401
from tests.test_api_missions import test_create_and_retrieve_mission, test_mission_timeline_is_chronological_and_uses_real_events  # noqa: E402,F401
from tests.test_orchestrator_persistence import (  # noqa: E402,F401
    test_full_run_persists_mission_step_agent_run_and_audit_trail,
    test_resume_completes_remaining_work_without_redispatching_completed_tasks,
)
from tests.test_phase13_selection import (  # noqa: E402,F401
    llm,
    orchestrator,
    test_changing_selection_supersedes_the_old_approval,
    test_selecting_a_safe_event_continues_the_same_mission_to_one_approval,
)
from tests.test_phase16_faculty_ops import (  # noqa: E402,F401
    app,
    client,
    clock,
    test_attendance_is_unique_per_student_and_session,
    test_event_permission_preview_confirm_approve_and_notify,
    test_session_lifecycle_start_mark_close_updates_counters_once,
)
from tests.test_phase15_auth_portal import test_dashboard_uses_real_records_and_deterministic_rules  # noqa: E402,F401
from tests.test_phase18_admin_ops import (  # noqa: E402,F401
    test_audit_log_merges_mission_and_operations_sources_read_only,
    test_hod_leave_goes_to_the_administration_and_is_decided_there,
)


# --- PostgreSQL-specific checks ------------------------------------------------------------------------


def test_schema_is_created_with_portable_types(engine) -> None:
    inspector = inspect(engine)
    assert len(inspector.get_table_names()) == 42
    assert missing_foreign_keys(engine) == []
    with engine.connect() as connection:
        column_type = connection.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'events' AND column_name = 'start_at'"
        )).scalar_one()
        native_enums = connection.execute(text(
            "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE t.typtype = 'e' AND n.nspname = current_schema()"
        )).scalar_one()
    assert column_type == "timestamp with time zone"
    assert native_enums == 0
    assert upgrade_schema(engine) == []  # idempotent


def test_aware_timestamps_round_trip_and_naive_is_rejected(session) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    session.execute(text("SET LOCAL TIME ZONE INTERVAL '-04:00' HOUR TO MINUTE"))  # the session's zone must not matter
    event = Event(title="TZ check", description="d", category="c", organizer="o", location="Hall", status=EventStatus.OPEN,
                  start_at=datetime(2026, 9, 30, 10, 0, tzinfo=ist), end_at=datetime(2026, 9, 30, 11, 0, tzinfo=ist))
    session.add(event)
    session.commit()
    session.expire_all()
    loaded = session.get(Event, event.id)
    assert loaded.start_at == datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc)
    assert loaded.start_at.tzinfo == timezone.utc
    session.add(Event(title="naive", description="d", category="c", organizer="o", location="Hall", status=EventStatus.OPEN,
                      start_at=datetime(2026, 9, 30, 10, 0), end_at=datetime(2026, 9, 30, 11, 0)))
    with pytest.raises(Exception, match="timezone-aware"):
        session.flush()
    session.rollback()


def test_seed_is_idempotent_and_keeps_the_demo_relationships(session_factory) -> None:
    with session_factory() as session:
        first = run_seed(session)
    with session_factory() as session:
        second = run_seed(session)
        aditi = session.execute(select(Student).where(Student.student_code == "STU-DEMO-001")).scalar_one()
        cse = session.execute(select(Department).where(Department.code == "CSE")).scalar_one()
        assert aditi.user.full_name == "Aditi Rao"
        assert cse.hod_faculty_id is not None
        assert first == second == build_summary(session)


def _operation_audit(event_type: str) -> OperationAuditEvent:
    return OperationAuditEvent(event_type=event_type, actor_role="system", subject_type="test", subject_id="1",
                               message="persisted", event_metadata={"k": [1, 2]})


def test_data_survives_an_engine_restart(engine) -> None:
    with create_session_factory(engine)() as session:
        run_seed(session)
        session.add(_operation_audit("pg_restart_check"))
        session.commit()
    engine.dispose()  # a fresh pool afterwards: new connections, nothing cached
    with create_session_factory(engine)() as session:
        row = session.execute(select(OperationAuditEvent).where(OperationAuditEvent.event_type == "pg_restart_check")).scalar_one()
        assert row.event_metadata == {"k": [1, 2]} and row.timestamp.tzinfo == timezone.utc
        assert session.execute(select(func.count()).select_from(Student)).scalar_one() > 0


def test_sqlite_to_postgres_copy_preserves_everything(tmp_path, engine) -> None:
    source = create_db_engine(db_path=tmp_path / "source.db")
    init_db(source)
    with create_session_factory(source)() as session:
        run_seed(session)
        session.add(_operation_audit("copied"))
        session.commit()
    report = copy_database(source, engine)
    assert report.inserted == sum(c.source for c in compare_counts(source, engine))
    assert all(c.ok for c in compare_counts(source, engine))
    assert find_orphans(engine) == []
    with create_session_factory(engine)() as session:
        cse = session.execute(select(Department).where(Department.code == "CSE")).scalar_one()
        assert cse.hod_faculty_id is not None  # the departments <-> faculty_profiles cycle is restored
        log = session.execute(select(OperationAuditEvent).where(OperationAuditEvent.event_type == "copied")).scalar_one()
        assert log.event_metadata == {"k": [1, 2]} and log.timestamp.tzinfo == timezone.utc
        # Sequences moved past the copied ids: a new row gets a fresh id.
        session.add(Department(code="NEW", name="New Department"))
        session.commit()
    source.dispose()


def test_mission_rows_are_written_to_postgres(api_client, session_factory) -> None:
    response = api_client.post("/missions", json={"student_id": "STU-DEMO-001", "goal": "Check my Operating Systems attendance"},
                               headers={"X-Demo-Identity": "student-demo"})
    assert response.status_code in (200, 201), response.text
    with session_factory() as session:
        assert session.get_bind().dialect.name == "postgresql"
        assert session.execute(select(func.count()).select_from(Mission)).scalar_one() >= 1


def test_every_connection_stays_in_its_throwaway_schema(engine) -> None:
    # SET search_path, not a startup option (Supabase's session pooler ignores those), and it survives
    # a rollback and a fresh pool.
    with engine.connect() as connection:
        schema = connection.execute(text("SELECT current_schema()")).scalar()
        assert schema.startswith("cn_test_") and schema != "public"
        connection.rollback()
        assert connection.execute(text("SELECT current_schema()")).scalar() == schema
    engine.dispose()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT current_schema()")).scalar() == schema
        assert "public" not in connection.execute(text("SHOW search_path")).scalar()


def test_data_api_roles_cannot_read_application_tables(engine) -> None:
    with engine.connect() as connection:
        roles = connection.execute(text("SELECT count(*) FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")).scalar_one()
    if roles < 2:
        pytest.skip("no Supabase anon/authenticated roles on this server")
    with engine.connect() as connection:
        protected = connection.execute(text(
            "SELECT bool_and(c.relrowsecurity) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind = 'r'")).scalar_one()
        readable = connection.execute(text(
            "SELECT bool_or(has_table_privilege(r, c.oid, 'SELECT')) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, unnest(ARRAY['anon', 'authenticated']) AS r "
            "WHERE n.nspname = current_schema() AND c.relkind = 'r'")).scalar_one()
    assert protected is True and readable is False


def test_health_reports_postgresql_without_connection_details(api_client) -> None:
    body = api_client.get("/health").json()
    assert body["database"]["dialect"] == "postgresql" and body["database"]["ready"] is True
    url = create_database_engine(postgres_test_url()).url
    rendered = str(body)
    for secret in (url.host, url.password, f":{url.port}", url.render_as_string(hide_password=False), "postgresql+psycopg"):
        if secret:
            assert str(secret) not in rendered
