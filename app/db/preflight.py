"""Phase 2.5: a secret-free database preflight and security check (schema level only, never row data).

Used by ``scripts/database_preflight.py``. Reports the mode, dialect, driver, a
redacted host description, connectivity (``SELECT 1``), the PostgreSQL version,
the required core and AgentOS tables, ``organization_id`` columns and tenant
indexes, the AgentOS workload indexes, the additive-upgrade state and, on a
server with Supabase's Data API roles, whether every application table is still
locked away from them. Nothing here changes the database; nothing printed holds
a URL, user, password, host name or Supabase project reference.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import PurePath
from typing import Any, Dict, List, Optional

from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import Connection, make_url

from app.db import models  # noqa: F401  (registers every mapped table)
from app.db.base import Base
from app.db.session import (
    SUPABASE_API_ROLES,
    DatabaseConfigError,
    create_database_engine,
    database_mode,
    get_database_url,
    is_supabase_host,
    plan_schema_upgrade,
    safe_error,
)

CORE_TABLES = ("organizations", "organization_memberships", "auth_accounts")
AGENTOS_TABLES = ("agent_missions", "agent_steps", "domain_events",
                  # Phase 3: the Assignment Guardian's domain
                  "assignments", "assignment_targets", "assignment_submissions", "assignment_followups",
                  # Phase 4: the Exam and Attendance Guardians' domains (``exams`` itself is a core academic table)
                  "exam_targets", "exam_attendance", "exam_followups", "attendance_interventions",
                  "attendance_followups",
                  # Phase 5: communication delivery
                  "communication_preferences", "contact_points", "communication_jobs", "communication_attempts",
                  "voice_sessions")
AGENTOS_INDEXES = (
    "ix_agent_missions_org_owner_agent", "ix_agent_missions_org_status_wake", "ix_domain_events_org_type_consumed",
    "ux_agent_steps_mission_step",
    "ix_assignments_org_status_deadline", "ix_assignment_followups_assignment_student_status",
    "ux_assignment_followups_active",
    "ix_exam_targets_org_student", "ix_exam_attendance_exam_status",
    "ix_exam_followups_exam_student_status", "ux_exam_followups_active", "ux_attendance_interventions_active",
    "ix_attendance_interventions_org_status", "ux_attendance_followups_active",
    "ux_communication_preferences_org_student", "ux_contact_points_org_student_kind", "ux_communication_jobs_source",
    "ix_communication_jobs_org_status_next", "ix_communication_attempts_provider_ref",
    "ix_communication_attempts_org_status", "ux_voice_sessions_attempt",
)
TENANT_COLUMN = "organization_id"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class PreflightReport:
    mode: str
    dialect: str = "unknown"
    driver: str = "unknown"
    host: str = "unknown"
    checks: List[Check] = field(default_factory=list)
    config_error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.config_error is None and bool(self.checks) and all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append(Check(name, ok, detail))
        return ok

    def as_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "ok": self.ok}

    def lines(self) -> List[str]:
        out = [f"Database mode: {self.mode}", f"Dialect: {self.dialect}", f"Driver: {self.driver}", f"Host: {self.host}"]
        if self.config_error:
            out.append(f"FAIL configuration: {self.config_error}")
        out.extend(f"{'OK  ' if c.ok else 'FAIL'} {c.name}: {c.detail}" for c in self.checks)
        out.append("Result: " + ("all required checks passed" if self.ok else "required checks FAILED"))
        return out


def safe_host(url: str) -> str:
    """Where the database is, without naming it: never the host name, user or Supabase project."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        return "in-memory" if parsed.database in (None, "", ":memory:") else f"local file {PurePath(parsed.database).name}"
    host, port = (parsed.host or "").lower(), parsed.port or 5432
    if host in ("localhost", "127.0.0.1", "::1"):
        return f"localhost:{port}"
    if is_supabase_host(host):
        if host.endswith(".pooler.supabase.com"):
            kind = {5432: "session pooler", 6543: "transaction pooler"}.get(port, f"pooler port {port}")
            return f"Supabase {kind} (port {port})"
        return f"Supabase direct connection (port {port})"
    return f"remote host (redacted), port {port}"


def _tenant_tables() -> List[str]:
    return [t.name for t in Base.metadata.sorted_tables if TENANT_COLUMN in t.columns]


def _leading_columns(connection: Connection, table: str) -> set[str]:
    inspector = inspect(connection)
    leading = {i["column_names"][0] for i in inspector.get_indexes(table) if i["column_names"]}
    leading |= {u["column_names"][0] for u in inspector.get_unique_constraints(table) if u["column_names"]}
    return leading


def _check_schema(report: PreflightReport, engine: Engine, connection: Connection) -> None:
    inspector = inspect(connection)
    present = set(inspector.get_table_names())
    for label, names in (("core tables", CORE_TABLES), ("AgentOS tables", AGENTOS_TABLES)):
        missing = [name for name in names if name not in present]
        report.add(label, not missing, "missing: " + ", ".join(missing) if missing else ", ".join(names))

    tenant = [name for name in _tenant_tables() if name in present]
    no_column = [name for name in tenant if TENANT_COLUMN not in {c["name"] for c in inspector.get_columns(name)}]
    report.add("organization_id columns", not no_column,
               f"missing on: {', '.join(no_column)}" if no_column else f"present on {len(tenant)} tenant tables")
    no_index = [name for name in tenant if name not in no_column and TENANT_COLUMN not in _leading_columns(connection, name)]
    report.add("tenant indexes", not no_index,
               f"no organization_id index on: {', '.join(no_index)}" if no_index else f"{len(tenant)} tenant tables indexed")

    indexes = {i["name"] for name in AGENTOS_TABLES if name in present for i in inspector.get_indexes(name)}
    missing_ix = [name for name in AGENTOS_INDEXES if name not in indexes]
    report.add("AgentOS indexes", not missing_ix, "missing: " + ", ".join(missing_ix) if missing_ix else "all present")

    plan = plan_schema_upgrade(engine)
    if plan.blocked_columns:
        report.add("schema upgrade state", False, "NOT NULL columns need a manual migration: " + ", ".join(plan.blocked_columns))
    elif plan.changes:
        report.add("schema upgrade state", False,
                   f"{len(plan.changes)} additive changes pending; run python scripts/upgrade_database.py")
    else:
        report.add("schema upgrade state", True, "current (no additive changes pending)")


def _check_data_api(report: PreflightReport, connection: Connection) -> None:
    """Supabase's anon/authenticated roles must not reach any application table (backend-only access)."""
    roles = sorted(connection.execute(
        text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"), {"names": list(SUPABASE_API_ROLES)}
    ).scalars())
    if not roles:
        report.add("Data API lockdown", True, "no anon/authenticated roles on this server (not Supabase)")
        return
    tables = [t.name for t in Base.metadata.sorted_tables]
    no_rls = list(connection.execute(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r' AND NOT c.relrowsecurity AND c.relname = ANY(:names) "
        "ORDER BY c.relname"), {"names": tables}).scalars())
    granted = list(connection.execute(text(
        "SELECT DISTINCT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
        "unnest(CAST(:roles AS text[])) AS r, unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) AS p "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r' AND c.relname = ANY(:names) "
        "AND has_table_privilege(r, c.oid, p) ORDER BY c.relname"), {"roles": roles, "names": tables}).scalars())
    # Any policy naming those roles (or PUBLIC) would reopen a table even with RLS on.
    policies = list(connection.execute(text(
        "SELECT DISTINCT tablename FROM pg_policies WHERE schemaname = current_schema() AND tablename = ANY(:names) "
        "AND roles && CAST(:roles AS name[]) ORDER BY tablename"), {"names": tables, "roles": [*roles, "public"]}).scalars())
    problems = ([f"RLS off: {', '.join(no_rls)}"] if no_rls else []) + \
        ([f"anon/authenticated privileges: {', '.join(granted)}"] if granted else []) + \
        ([f"policies for anon/authenticated/public: {', '.join(policies)}"] if policies else [])
    report.add("Data API lockdown", not problems, "; ".join(problems) if problems else
               "RLS on, no anon/authenticated privileges or policies on any application table")


def run_preflight(url: Optional[str] = None, *, engine: Optional[Engine] = None) -> PreflightReport:
    """Run every check against ``engine``, ``url`` or else the configured database. Never raises for a database problem.

    A given ``engine`` is used as is (e.g. one confined to a schema) and not disposed.
    """
    owned = engine is None
    try:
        if engine is not None:
            url = engine.url.render_as_string(hide_password=False)
        mode = database_mode() if url is None else ("postgres" if url.startswith("postgresql") else "local")
        url = url or get_database_url()
    except DatabaseConfigError as exc:
        return PreflightReport(mode="invalid", config_error=str(exc))
    parsed = make_url(url)
    report = PreflightReport(mode=mode, dialect=parsed.get_backend_name(), driver=parsed.get_driver_name(),
                             host=safe_host(url))
    try:
        engine = engine if engine is not None else create_database_engine(url)
    except DatabaseConfigError as exc:
        report.config_error = str(exc)
        return report
    except ImportError as exc:  # the driver's module or native library cannot load
        report.add("driver", False, f"{report.driver} cannot be imported ({type(exc).__name__}: {str(exc)[:120]})")
        return report
    report.add("driver", True, f"{report.driver} loaded")
    try:
        with engine.connect() as connection:
            value = connection.execute(text("SELECT 1")).scalar()
            if not report.add("connectivity", value == 1, f"SELECT 1 returned {value}"):
                return report
            if report.dialect == "postgresql":
                version = connection.execute(text("SHOW server_version")).scalar()
                report.add("server version", True, f"PostgreSQL {version}")
            _check_schema(report, engine, connection)
            if report.dialect == "postgresql":
                _check_data_api(report, connection)
    except Exception as exc:  # noqa: BLE001 -- report the failure without the URL, host or user
        report.add("connectivity" if not report.checks[1:] else "schema checks", False, safe_error(exc, url))
    finally:
        if owned:
            engine.dispose()
    return report
