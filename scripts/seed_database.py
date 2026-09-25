"""Seed CampusNexus into SQLite or PostgreSQL / Supabase (Phase 21).

Targets the configured database (``CAMPUSNEXUS_DATABASE_URL``, else
``CAMPUSNEXUS_DB_PATH``, else ``data/campusnexus.db``), or ``--sqlite PATH``.
Creates missing tables, then runs the same seed as ``scripts/seed_data.py``
plus the development sign-in accounts.

Idempotent on any day: the base seed runs only while the demo student
(STU-DEMO-001) does not exist yet. ``seed_data.run_seed`` itself is idempotent
only within one day (its dates are relative to "now", so a later run would add
a second copy of the exams), so an already-seeded database is left as it is
and only the accounts and demo classes are ensured. Seed dates therefore stay
those of the first run; refreshing them means a reset. ``--demo-classes`` also schedules the
Phase 16/17 extra classes.

Nothing is deleted unless ``--reset`` is given. Resetting a PostgreSQL database
additionally needs ``--allow-remote-reset`` and ``--confirm-database <name>``
matching the target database name, which is printed first. The URL, host, user
and password are never printed.

Usage:
    python scripts/seed_database.py
    python scripts/seed_database.py --demo-classes
    python scripts/seed_database.py --reset --allow-remote-reset --confirm-database postgres
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.auth.accounts import DEV_ACCOUNTS, resolve_seed_password, seed_dev_accounts  # noqa: E402
from app.db.models import Student  # noqa: E402
from app.db.session import (  # noqa: E402
    DatabaseConfigError,
    create_database_engine,
    create_session_factory,
    describe_database,
    drop_db,
    get_database_url,
    init_db,
    safe_error,
)
from scripts.seed_data import DEMO_STUDENT_CODE, build_summary, run_seed  # noqa: E402

SQLITE_CREDENTIALS = REPO_ROOT / "data" / "dev_credentials.txt"
# Shared with the local demo, so a PostgreSQL database migrated from data/demo/ (which carries its
# password hashes) and one seeded here both match the same git-ignored file.
POSTGRES_CREDENTIALS = REPO_ROOT / "data" / "demo" / "dev_credentials.txt"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed CampusNexus into the configured SQLite or PostgreSQL database.")
    parser.add_argument("--sqlite", metavar="PATH", help="Seed this SQLite file instead of the configured database.")
    parser.add_argument("--demo-classes", action="store_true", help="Also schedule the Phase 16/17 extra demo classes.")
    parser.add_argument("--reset", action="store_true", help="Drop every application table first (destructive).")
    parser.add_argument("--allow-remote-reset", action="store_true", help="Required with --reset on PostgreSQL.")
    parser.add_argument("--confirm-database", metavar="NAME", help="Required with --reset on PostgreSQL: the target database name.")
    return parser.parse_args(argv)


def target_description(url: str) -> str:
    parsed = make_url(url)
    backend = describe_database(parsed)
    if backend["dialect"] == "postgresql":
        return f"{backend['label']} database '{parsed.database}'"
    return f"SQLite {parsed.database}"


def reset_refusal(url: str, args: argparse.Namespace) -> str | None:
    """Why a --reset must not run against ``url``, or None when it may."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        return None
    if not args.allow_remote_reset:
        return "Refusing to reset a PostgreSQL database without --allow-remote-reset."
    if args.confirm_database != parsed.database:
        return f"Refusing to reset: pass --confirm-database {parsed.database} to confirm the target."
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        url = get_database_url(args.sqlite) if args.sqlite else get_database_url()
    except DatabaseConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    postgres = make_url(url).get_backend_name() == "postgresql"
    print(f"Target: {target_description(url)}")
    if args.reset:
        refusal = reset_refusal(url, args)
        if refusal:
            print(f"ERROR: {refusal}")
            return 2

    engine = create_database_engine(url)
    try:
        if args.reset:
            drop_db(engine)
            print("Reset: every application table dropped")
        init_db(engine)
        password, password_source = resolve_seed_password(POSTGRES_CREDENTIALS if postgres else SQLITE_CREDENTIALS)
        with create_session_factory(engine)() as session:
            already_seeded = session.execute(
                select(Student.id).where(Student.student_code == DEMO_STUDENT_CODE)
            ).first() is not None
            if not already_seeded:
                run_seed(session)
            created = seed_dev_accounts(session, password)
            summary = build_summary(session)
            classes = []
            if args.demo_classes:
                from scripts.schedule_demo_class import schedule_extra_class, schedule_tomorrow_afternoon

                classes = [schedule_extra_class(session), schedule_tomorrow_afternoon(session)]
    except Exception as exc:  # noqa: BLE001 -- report without leaking the URL
        print(f"ERROR: {safe_error(exc, url)}")
        return 1
    finally:
        engine.dispose()

    if already_seeded:
        print("Base seed skipped: the database is already seeded (dates stay those of the first seed).")
    print("Seed complete. Row counts:")
    for name, value in summary.__dict__.items():
        print(f"  {name}: {value}")
    print(f"Development sign-in accounts: {len(created)} new; password from {password_source}")
    for spec in DEV_ACCOUNTS:
        print(f"  {spec.email} ({spec.role.value})")
    if classes:
        print(f"Extra demo classes scheduled: {len(classes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
