"""Initialize the CampusNexus schema in PostgreSQL / Supabase (Phase 21).

Reads ``CAMPUSNEXUS_DATABASE_URL`` and, in order:
  1. confirms it names a PostgreSQL database (psycopg 3 driver)
  2. tests the connection
  3. creates the application tables that are missing (from the SQLAlchemy models)
  4. runs the additive schema upgrade (new nullable columns; never drops anything)
  5. keeps the tables out of Supabase's Data API (row level security on,
     anon/authenticated privileges revoked) where those roles exist
  6. verifies the critical tables and their foreign keys

Never drops, truncates or resets anything, and never prints the URL, host,
user or password. Safe to re-run.

Usage (PowerShell):
    $env:CAMPUSNEXUS_DATABASE_URL = "<Supabase session pooler URI>"
    python scripts/init_postgres_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import inspect, text  # noqa: E402

from app.db.portability import missing_foreign_keys  # noqa: E402
from app.db.session import (  # noqa: E402
    DatabaseConfigError,
    configured_postgres_url,
    create_database_engine,
    describe_database,
    init_db,
    safe_error,
)

CRITICAL_TABLES = (
    "departments", "users", "students", "faculty_profiles", "courses", "enrollments", "attendance_records",
    "attendance_sessions", "session_attendance_marks", "events", "event_registrations", "workflow_requests",
    "notifications", "staff_notifications", "account_notifications", "missions", "mission_steps",
    "approval_records", "audit_logs", "operation_audit_events", "action_candidates", "target_selections",
    "auth_accounts", "organizations", "organization_memberships",
)


def main() -> int:
    try:
        url = configured_postgres_url()
    except DatabaseConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    backend = describe_database(url)
    print(f"Database: {backend['label']}")
    engine = create_database_engine(url)
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SHOW server_version")).scalar()
        print(f"Connection: ok (server {version})")

        before = set(inspect(engine).get_table_names())
        init_db(engine)
        after = set(inspect(engine).get_table_names())
        created = sorted(after - before)
        print(f"Tables: {len(after)} present; {len(created)} created now" + (f" ({', '.join(created)})" if created else ""))

        missing = [name for name in CRITICAL_TABLES if name not in after]
        fk_gaps = missing_foreign_keys(engine)
        with engine.connect() as connection:
            api_roles = connection.execute(
                text("SELECT count(*) FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")
            ).scalar_one()
            unprotected = connection.execute(text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind = 'r' AND NOT c.relrowsecurity "
                "AND c.relname = ANY(:names)"), {"names": sorted(after)}).scalar_one()
        if api_roles:
            print(f"Data API: {'locked down' if not unprotected else f'{unprotected} tables WITHOUT row level security'} "
                  "(application tables are reachable only through FastAPI)")
        else:
            print("Data API: no anon/authenticated roles on this server (not Supabase); nothing to lock down")
    except Exception as exc:  # noqa: BLE001 -- report without leaking the URL
        print(f"ERROR: {safe_error(exc, url)}")
        return 1
    finally:
        engine.dispose()

    if missing:
        print(f"ERROR: critical tables missing: {', '.join(missing)}")
        return 1
    if fk_gaps:
        print(f"ERROR: foreign keys missing: {', '.join(fk_gaps)}")
        return 1
    if api_roles and unprotected:
        return 1
    print(f"Critical tables: all {len(CRITICAL_TABLES)} present with their foreign keys")
    print("Schema ready. Next: python scripts/seed_database.py  (or scripts/migrate_sqlite_to_postgres.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
