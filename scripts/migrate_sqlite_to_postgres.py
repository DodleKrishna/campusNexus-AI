"""Copy a CampusNexus SQLite database into PostgreSQL / Supabase (Phase 21).

Source: ``--source PATH`` (a SQLite file; opened read-only, never modified).
Destination: ``CAMPUSNEXUS_DATABASE_URL`` (must be PostgreSQL).

Steps: check both databases -> create the target schema if needed (additive,
same as scripts/init_postgres_db.py) -> validate the source (values that would
not fit PostgreSQL's VARCHAR lengths, orphan rows, unknown enum values) ->
refuse if the target already holds data -> copy every application table
parent-first in one transaction, keeping primary keys, timestamps and JSON ->
move id sequences past the copied ids -> compare source/target row counts for
every table -> check every foreign key for orphans.

A populated target is never overwritten. ``--merge-identical`` is the only
exception, and it only adds rows whose primary key is missing: it refuses if
any existing target row differs from, or is absent in, the source (so re-running
a finished migration is a safe no-op).

``--dry-run`` does every check and prints the plan without writing anything
(the target schema is not created either). Never prints the URL, host, user or
password.

Usage (PowerShell):
    $env:CAMPUSNEXUS_DATABASE_URL = "<Supabase session pooler URI>"
    python scripts/migrate_sqlite_to_postgres.py --source data/demo/campusnexus_demo.db --dry-run
    python scripts/migrate_sqlite_to_postgres.py --source data/demo/campusnexus_demo.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import Engine  # noqa: E402

from app.db.portability import (  # noqa: E402
    MigrationError,
    compare_counts,
    copy_database,
    find_orphans,
    missing_foreign_keys,
    summarize_counts,
)
from app.db.session import (  # noqa: E402
    DatabaseConfigError,
    configured_postgres_url,
    create_database_engine,
    describe_database,
    init_db,
    safe_error,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy a CampusNexus SQLite database into PostgreSQL.")
    parser.add_argument("--source", required=True, help="The SQLite database file to copy from (read-only).")
    parser.add_argument("--dry-run", action="store_true", help="Check and report only; write nothing.")
    parser.add_argument(
        "--merge-identical", action="store_true",
        help="Allow a non-empty target: add only missing rows; refuse if any existing row differs.",
    )
    return parser.parse_args(argv)


def open_sqlite_read_only(path: Path) -> Engine:
    return create_database_engine(f"sqlite:///file:{path.resolve().as_posix()}?mode=ro&uri=true")


def verify(source: Engine, target: Engine) -> bool:
    """Row counts per table plus relational integrity on the target. Prints the report."""
    checks = compare_counts(source, target)
    print("\n".join(summarize_counts(checks)))
    orphans = find_orphans(target)
    fk_gaps = missing_foreign_keys(target)
    mismatched = [c.table for c in checks if not c.ok]
    print(f"Counts: {len(checks) - len(mismatched)}/{len(checks)} tables match "
          f"({sum(c.source for c in checks)} source rows, {sum(c.target for c in checks)} target rows)")
    print("Integrity: " + ("no orphan rows" if not orphans else "; ".join(
        f"{o.table}.{','.join(o.columns)} -> {o.parent}: {o.rows} orphans" for o in orphans)))
    print("Foreign keys: " + ("every model foreign key exists in PostgreSQL" if not fk_gaps else f"missing on {', '.join(fk_gaps)}"))
    return not mismatched and not orphans and not fk_gaps


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_path = Path(args.source)
    if not source_path.is_file():
        print(f"ERROR: source SQLite database not found: {source_path}")
        return 2
    try:
        url = configured_postgres_url()
    except DatabaseConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"Source: SQLite {source_path}")
    print(f"Target: {describe_database(url)['label']}" + ("  [dry run: nothing will be written]" if args.dry_run else ""))

    source = open_sqlite_read_only(source_path)
    target = create_database_engine(url)
    try:
        if not args.dry_run:
            init_db(target)
        report = copy_database(source, target, dry_run=args.dry_run, merge_identical=args.merge_identical)
        if report.ignored_source_tables:
            print(f"Ignored source tables (not application tables): {', '.join(report.ignored_source_tables)}")
        width = max(len(t.table) for t in report.tables)
        verb = "would copy" if args.dry_run else "copied"
        for entry in report.tables:
            if entry.source_rows or entry.target_rows_before:
                existing = f" (target already had {entry.target_rows_before})" if entry.target_rows_before else ""
                print(f"  {entry.table:<{width}}  {verb} {entry.inserted}{existing}")
        print(f"Rows {verb}: {report.inserted} across {sum(1 for t in report.tables if t.inserted)} tables")
        if args.dry_run:
            print("Dry run complete: source validated, target has no conflicting data.")
            return 0
        print(f"Sequences reset: {len(report.sequences_reset)} tables")
        print("\nVerification")
        return 0 if verify(source, target) else 1
    except MigrationError as exc:
        print(f"REFUSED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 -- the transaction rolled back; report without leaking the URL
        print(f"ERROR (nothing committed): {safe_error(exc, url)}")
        return 1
    finally:
        source.dispose()
        target.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
