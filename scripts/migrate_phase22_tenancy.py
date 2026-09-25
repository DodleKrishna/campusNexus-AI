"""Phase 22A: move an existing single-institution CampusNexus database onto organizations.

Target: ``--sqlite PATH``, else the configured database (``CAMPUSNEXUS_DATABASE_URL``,
then ``CAMPUSNEXUS_DB_PATH``), exactly like scripts/seed_database.py.

Steps:
  1. additive schema upgrade (``upgrade_schema``): the ``organizations`` and
     ``organization_memberships`` tables, a nullable ``organization_id`` (with its
     foreign key and index) on every tenant table, the per-organization indexes;
  2. in ONE transaction: create the demo organization if missing, give it every
     row that has no organization, create each account's membership from its
     role and profile links, then run the integrity report -- rows without an
     organization, cross-organization references, accounts without an active
     membership, role/profile drift. Any problem rolls the whole transaction back.

Refuses to attribute rows when the database already holds more than one
organization. Never deletes or rewrites existing data, and never prints the
URL, host, user or password. Safe to re-run (a finished migration is a no-op).
``NOT NULL`` on ``organization_id`` is deliberately not enforced yet: until the
Phase 22B tenant session exists, the running application still writes rows
without an organization, which a re-run of this script assigns.

``--dry-run`` only reads: it reports what the upgrade and the backfill would do.

Usage (PowerShell):
    python scripts/migrate_phase22_tenancy.py --dry-run
    python scripts/migrate_phase22_tenancy.py
    python scripts/migrate_phase22_tenancy.py --sqlite data/demo/campusnexus_demo.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import Engine, func, inspect, select, text  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.models.auth import AuthAccount  # noqa: E402
from app.db.session import (  # noqa: E402
    DatabaseConfigError,
    create_database_engine,
    create_session_factory,
    describe_database,
    get_database_url,
    safe_error,
    upgrade_schema,
)
from app.db.tenancy import (  # noqa: E402
    DEFAULT_ORGANIZATION_SLUG,
    ORGANIZATION_COLUMN,
    TenancyError,
    _profile_links,
    assign_unowned_rows,
    ensure_default_organization,
    sync_memberships,
    tenancy_report,
    tenant_tables,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move an existing CampusNexus database onto organizations (Phase 22A).")
    parser.add_argument("--sqlite", metavar="PATH", help="Migrate this SQLite file instead of the configured database.")
    parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing (not even the schema upgrade).")
    return parser.parse_args(argv)


def plan(engine: Engine) -> int:
    """Read-only report of what the migration would do. Returns the exit code."""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    new_tables = [t.name for t in Base.metadata.sorted_tables if t.name not in existing]
    missing_columns = [t.name for t in tenant_tables() if t.name in existing
                       and ORGANIZATION_COLUMN not in {c["name"] for c in inspector.get_columns(t.name)}]
    print(f"Schema: {len(new_tables)} table(s) to create" + (f" ({', '.join(new_tables)})" if new_tables else ""))
    print(f"Schema: organization_id to add on {len(missing_columns)} existing table(s)")
    organizations = 0
    with engine.connect() as connection:
        if "organizations" in existing:
            organizations = connection.execute(text("SELECT count(*) FROM organizations")).scalar_one()
        to_assign = {}
        for table in tenant_tables():
            if table.name not in existing:
                continue
            where = "" if table.name in missing_columns else f' WHERE "{ORGANIZATION_COLUMN}" IS NULL'
            count = connection.execute(text(f'SELECT count(*) FROM "{table.name}"{where}')).scalar_one()
            if count:
                to_assign[table.name] = count
    print(f"Organizations present: {organizations}" + ("" if organizations else f" (would create '{DEFAULT_ORGANIZATION_SLUG}')"))
    print(f"Rows to assign to '{DEFAULT_ORGANIZATION_SLUG}': {sum(to_assign.values())} across {len(to_assign)} table(s)")
    if organizations > 1 and to_assign:
        print("REFUSED: more than one organization exists, so rows without one cannot be attributed.")
        return 1
    members = set()
    if "organization_memberships" in existing:
        with engine.connect() as connection:
            members = set(connection.execute(text("SELECT account_id FROM organization_memberships")).scalars())
    with create_session_factory(engine)() as session:
        accounts = session.execute(select(AuthAccount).order_by(AuthAccount.id)).scalars().all() if "auth_accounts" in existing else []
        pending = [a for a in accounts if a.id not in members]
        problems = [(a.email, _profile_links(session, a)[2]) for a in pending]
        problems = [(email, reason) for email, reason in problems if reason]
    print(f"Memberships to create: {len(pending) - len(problems)} of {len(pending)} account(s) without one")
    for email, reason in problems:
        print(f"  would stop: {email}: {reason}")
    print("Dry run complete: nothing was written.")
    return 1 if problems else 0


def migrate(engine: Engine) -> int:
    added = upgrade_schema(engine)
    print(f"Schema upgrade: {len(added)} addition(s)" + (f" ({len([a for a in added if a.startswith('index:')])} indexes)" if added else ""))
    session = create_session_factory(engine)()
    try:
        organization = ensure_default_organization(session)
        assigned = assign_unowned_rows(session, organization)
        memberships = sync_memberships(session, organization)
        report = tenancy_report(session)
        print(f"Organization: '{organization.slug}' (id {organization.id})")
        print(f"Rows assigned: {sum(assigned.values())} across {len(assigned)} table(s)")
        print(f"Memberships created: {len(memberships.created)}")
        for email, reason in memberships.skipped:
            print(f"  skipped {email}: {reason}")
        print("Integrity:")
        for line in report.lines():
            print(f"  {line}")
        if not report.ok:
            session.rollback()
            print("ROLLED BACK: fix the problems above and re-run (the additive schema upgrade stays).")
            return 1
        session.commit()
        counts = {t.name: session.execute(select(func.count()).select_from(t)).scalar_one() for t in tenant_tables()}
        print(f"Committed. Tenant rows now owned: {sum(counts.values())} across {len(counts)} tables")
        return 0
    except TenancyError as exc:
        session.rollback()
        print(f"REFUSED: {exc}")
        return 1
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        url = get_database_url(args.sqlite) if args.sqlite else get_database_url()
    except DatabaseConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"Target: {describe_database(url)['label']}" + ("  [dry run]" if args.dry_run else ""))
    engine = create_database_engine(url)
    try:
        return plan(engine) if args.dry_run else migrate(engine)
    except Exception as exc:  # noqa: BLE001 -- report without leaking the URL
        print(f"ERROR (the backfill transaction was not committed): {safe_error(exc, url)}")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
