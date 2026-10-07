"""Database preflight + security check (Phase 2.5). Read-only; never prints a URL, user, password or host name.

Checks the configured database (CAMPUSNEXUS_DATABASE_MODE / CAMPUSNEXUS_DATABASE_URL / CAMPUSNEXUS_DB_PATH):
mode, dialect, driver, a redacted host, SELECT 1, the PostgreSQL version, the core and AgentOS tables,
organization_id columns and tenant indexes, the AgentOS indexes, the schema upgrade state and (on Supabase)
the Data API lockdown. See app/db/preflight.py.

Exit codes: 0 every check passed, 1 a check failed, 2 the configuration is invalid.

Usage (PowerShell):
    $env:CAMPUSNEXUS_DATABASE_MODE = "postgres"
    $env:CAMPUSNEXUS_DATABASE_URL = "<Supabase session pooler URI, port 5432>"
    python scripts/database_preflight.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db.preflight import run_preflight  # noqa: E402


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = run_preflight()
    print(json.dumps(report.as_dict(), indent=2) if args.json else "\n".join(report.lines()))
    if report.config_error:
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
