"""Deliberately bring the configured database to the current schema (Phase 2.5). Additive only.

Runs ``app.db.session.upgrade_schema``: creates missing tables, adds missing nullable columns (with their foreign
keys) and missing indexes, and on PostgreSQL keeps every application table out of Supabase's Data API. One
transaction (PostgreSQL DDL is transactional, so a failure changes nothing) under an advisory lock. Never drops,
renames, truncates or rewrites anything. The API never runs this against PostgreSQL at startup; it only verifies.

Usage (PowerShell):
    python scripts/upgrade_database.py --dry-run     # list the pending changes, change nothing
    python scripts/upgrade_database.py               # apply them, then run the preflight

Exit codes: 0 done (or nothing to do), 1 failed or the preflight failed, 2 invalid configuration.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db import models  # noqa: E402,F401  (registers every mapped table)
from app.db.preflight import run_preflight, safe_host  # noqa: E402
from app.db.session import (  # noqa: E402
    DatabaseConfigError,
    create_database_engine,
    database_mode,
    get_database_url,
    plan_schema_upgrade,
    safe_error,
    upgrade_schema,
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="list the pending additive changes; change nothing")
    args = parser.parse_args(argv)
    try:
        mode, url = database_mode(), get_database_url()
        engine = create_database_engine(url)
    except DatabaseConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"Database mode: {mode} ({engine.dialect.name}+{engine.dialect.driver}, {safe_host(url)})")
    try:
        plan = plan_schema_upgrade(engine)
        if plan.blocked_columns:
            print("NOT NULL columns an additive upgrade cannot add (manual migration needed): " + ", ".join(plan.blocked_columns))
        if not plan.changes:
            print("Schema: current; nothing to add.")
        else:
            print(f"Pending additive changes ({len(plan.changes)}):")
            for change in plan.changes:
                print(f"  + {change}")
        if args.dry_run:
            return 1 if plan.blocked_columns else 0
        if plan.changes or engine.dialect.name == "postgresql":  # PostgreSQL: also re-checks the Data API lockdown
            applied = upgrade_schema(engine)
            print(f"Applied {len(applied)} changes.")
    except Exception as exc:  # noqa: BLE001 -- report without the URL, host or user
        print(f"ERROR: {safe_error(exc, url)}")
        return 1
    finally:
        engine.dispose()
    report = run_preflight(url)
    print("\n".join(report.lines()))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
