"""Engine/session creation and database initialization.

No module-level global engine or session is kept here -- every caller
(scripts/seed_data.py, the Context Service, tests) explicitly creates its own
engine and session factory. This keeps tests fully isolated from the
development database and from each other: a test that wants a throwaway
database just points ``CAMPUSNEXUS_DB_PATH`` (or the ``db_path`` argument) at
a temp file or ``:memory:`` before building its own engine.

Phase 21: the same models run on SQLite (local/offline/tests) or PostgreSQL
(Supabase, the cloud/product database). ``get_database_url`` picks the target
and ``create_database_engine`` is the single dialect-aware engine factory.
Creating an engine never connects; nothing here runs at import time.

Phase 2.5: ``CAMPUSNEXUS_DATABASE_MODE`` names the mode explicitly. ``local`` is
SQLite (offline edge, tests); ``postgres`` requires a PostgreSQL
``CAMPUSNEXUS_DATABASE_URL`` and never falls back to SQLite. The URL's driver
(``+psycopg`` or ``+pg8000``) is used as written. ``plan_schema_upgrade`` reports
what ``upgrade_schema`` would add without changing anything.
"""
from __future__ import annotations

import os
import re
import ssl
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

from sqlalchemy import Column, Engine, Index, Table, create_engine, event, inspect, text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base

# Project root: .../app/db/session.py -> parents[2] == repo root.
BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = BASE_DIR / "data" / "campusnexus.db"

DATABASE_URL_ENV = "CAMPUSNEXUS_DATABASE_URL"
DB_PATH_ENV = "CAMPUSNEXUS_DB_PATH"
DATABASE_MODE_ENV = "CAMPUSNEXUS_DATABASE_MODE"
MODE_LOCAL, MODE_POSTGRES = "local", "postgres"
DATABASE_MODES = (MODE_LOCAL, MODE_POSTGRES)
# Default PostgreSQL driver for a URL that names none (``postgresql://``, the form Supabase shows).
POSTGRES_DRIVER = "postgresql+psycopg"
# Supported PostgreSQL drivers. pg8000 is pure Python, for hosts where psycopg's native libpq cannot load.
POSTGRES_DRIVERS = ("psycopg", "pg8000")
SUPABASE_HOST_SUFFIXES = (".supabase.com", ".supabase.co")
# Roles through which Supabase's Data API (PostgREST) reaches the database.
SUPABASE_API_ROLES = ("anon", "authenticated")
# Serializes concurrent schema upgrades on PostgreSQL (any constant; held only for the upgrade transaction).
_UPGRADE_LOCK_KEY = 0x434E5553


class DatabaseConfigError(ValueError):
    """The database configuration is missing, contradictory or names an unsupported database/driver."""


class DatabaseUnavailableError(RuntimeError):
    """The configured database cannot be reached or its schema is not current. The message is secret-free."""


def _sqlite_url(raw: str | Path) -> str:
    raw = str(raw)
    if raw == ":memory:":
        return "sqlite:///:memory:"
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def normalize_database_url(raw: str) -> str:
    """Accept a SQLite or PostgreSQL URL and return the SQLAlchemy URL to use.

    ``postgresql://``/``postgres://`` (no driver named) use psycopg 3. An explicit
    ``+psycopg`` or ``+pg8000`` driver is kept as written, never rewritten. Any
    other dialect or driver is refused rather than guessed.
    """
    try:
        url = make_url(raw.strip())
    except Exception as exc:  # noqa: BLE001 -- never echo the value, it may hold a password
        raise DatabaseConfigError(f"{DATABASE_URL_ENV} is not a valid database URL") from exc
    backend, _, driver = url.drivername.partition("+")
    if backend in ("postgresql", "postgres"):
        if driver and driver not in POSTGRES_DRIVERS:
            raise DatabaseConfigError(
                f"Unsupported PostgreSQL driver '{driver}'; use postgresql+psycopg:// or postgresql+pg8000://")
        return url.set(drivername=f"postgresql+{driver or 'psycopg'}").render_as_string(hide_password=False)
    if backend == "sqlite":
        if url.database in (None, "", ":memory:"):
            return "sqlite:///:memory:"
        return _sqlite_url(url.database)
    raise DatabaseConfigError(
        f"Unsupported database '{backend}'; use sqlite:///, postgresql+psycopg:// or postgresql+pg8000://")


def database_mode() -> str:
    """``local`` (SQLite) or ``postgres``, from ``CAMPUSNEXUS_DATABASE_MODE``.

    Unset keeps the Phase 21 behaviour: a PostgreSQL ``CAMPUSNEXUS_DATABASE_URL``
    means ``postgres``, anything else ``local``. An unknown value is refused.
    """
    raw = os.environ.get(DATABASE_MODE_ENV, "").strip().lower()
    if raw:
        if raw not in DATABASE_MODES:
            raise DatabaseConfigError(f"{DATABASE_MODE_ENV} must be one of: {', '.join(DATABASE_MODES)}")
        return raw
    configured = os.environ.get(DATABASE_URL_ENV, "").strip()
    return MODE_POSTGRES if configured and normalize_database_url(configured).startswith("postgresql+") else MODE_LOCAL


def get_database_url(db_path: str | Path | None = None) -> str:
    """Resolve the database URL to use.

    An explicit ``db_path`` argument is always that SQLite file (tests, evals and
    the local demo reset pin their own file this way). Otherwise, by mode:

    * ``postgres``: ``CAMPUSNEXUS_DATABASE_URL``, which must be set and name
      PostgreSQL. There is no fallback to SQLite.
    * ``local``: a SQLite ``CAMPUSNEXUS_DATABASE_URL``, then
      ``CAMPUSNEXUS_DB_PATH``, then ``data/campusnexus.db``. A PostgreSQL URL
      under an explicit ``local`` mode is refused as contradictory.

    ``:memory:`` is passed through as an in-memory SQLite database.
    """
    if db_path is not None:
        return _sqlite_url(db_path)
    mode = database_mode()
    configured = os.environ.get(DATABASE_URL_ENV, "").strip()
    if mode == MODE_POSTGRES:
        if not configured:
            raise DatabaseConfigError(f"{DATABASE_MODE_ENV}=postgres requires {DATABASE_URL_ENV}")
        url = normalize_database_url(configured)
        if not url.startswith("postgresql+"):
            raise DatabaseConfigError(f"{DATABASE_MODE_ENV}=postgres requires a PostgreSQL {DATABASE_URL_ENV}")
        return url
    if configured:
        url = normalize_database_url(configured)
        if url.startswith("postgresql+"):
            raise DatabaseConfigError(
                f"{DATABASE_MODE_ENV}=local uses SQLite; unset {DATABASE_URL_ENV} or set {DATABASE_MODE_ENV}=postgres")
        return url
    return _sqlite_url(os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite ignores FOREIGN KEY constraints unless enabled per-connection."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    """A bounded integer setting; anything else is refused (a pool must never become unbounded)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise DatabaseConfigError(f"{name} must be an integer from {low} to {high}")
    return int(raw)


def is_supabase_host(host: Optional[str]) -> bool:
    return (host or "").lower().endswith(SUPABASE_HOST_SUFFIXES)


_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
_SSL_REQUIRED = ("require", "verify-ca", "verify-full")


def _query_value(url: URL, name: str) -> Optional[str]:
    raw = url.query.get(name)
    return raw[-1] if isinstance(raw, tuple) else raw


def _ssl_mode(url: URL) -> Optional[str]:
    """The URL's ``sslmode``; Supabase hosts must use SSL (``require`` when unset, weaker modes refused)."""
    mode = (_query_value(url, "sslmode") or "").strip().lower() or None
    if mode is not None and mode not in _SSL_MODES:
        raise DatabaseConfigError(f"Unsupported sslmode; use one of: {', '.join(_SSL_MODES)}")
    if is_supabase_host(url.host):
        if mode is None:
            return "require"
        if mode not in _SSL_REQUIRED:
            raise DatabaseConfigError("Supabase connections require SSL: use sslmode=require (or verify-ca/verify-full)")
    return mode


def _pg8000_ssl_context(mode: Optional[str], root_cert: Optional[str]) -> Optional[ssl.SSLContext]:
    """libpq ``sslmode`` semantics for pg8000, which takes an ``ssl_context`` instead."""
    if mode in (None, "disable"):
        return None
    if mode in ("allow", "prefer"):
        raise DatabaseConfigError("postgresql+pg8000 supports sslmode disable, require, verify-ca or verify-full")
    context = ssl.create_default_context(cafile=root_cert or None)
    if mode != "verify-full":
        context.check_hostname = False
    if mode == "require":  # encrypted, certificate not verified (as libpq's require)
        context.verify_mode = ssl.CERT_NONE
    return context


def _postgres_connect(url: URL) -> Tuple[URL, dict[str, Any]]:
    """Driver-specific connection settings: finite timeouts, an application name and the SSL mode."""
    driver = url.get_driver_name()
    mode = _ssl_mode(url)
    if driver == "psycopg":
        # Server-side prepared statements off, so the same URL also works behind a transaction-mode pooler.
        args: dict[str, Any] = {"connect_timeout": _env_int("CAMPUSNEXUS_DB_CONNECT_TIMEOUT", 15, 1, 120),
                                "application_name": "campusnexus", "prepare_threshold": None}
        if mode is not None and "sslmode" not in url.query:
            args["sslmode"] = mode
        return url, args
    if driver == "pg8000":
        # pg8000's timeout is a socket timeout: it bounds the connect and every read.
        args = {"timeout": _env_int("CAMPUSNEXUS_DB_SOCKET_TIMEOUT", 60, 5, 600), "application_name": "campusnexus"}
        context = _pg8000_ssl_context(mode, _query_value(url, "sslrootcert"))
        if context is not None:
            args["ssl_context"] = context
        return url.difference_update_query(["sslmode", "sslrootcert"]), args
    raise DatabaseConfigError("Unsupported PostgreSQL driver; use postgresql+psycopg:// or postgresql+pg8000://")


def create_database_engine(url: str | URL, *, echo: bool = False, connect_args: dict[str, Any] | None = None) -> Engine:
    """The one dialect-aware engine factory. Never connects by itself. Build one per process, never per request.

    SQLite: ``check_same_thread=False`` (FastAPI's threadpool shares the engine)
    and foreign keys switched on per connection.

    PostgreSQL (psycopg 3 or pg8000, as the URL names): a small, bounded,
    pre-pinged pool sized for Supabase's Session pooler (port 5432, IPv4, fit for
    a persistent backend), recycled before idle connections are dropped, with a
    finite checkout and connect/socket timeout. Supabase hosts always use SSL.
    Nothing Supabase-specific (host, project, password) is hardcoded.
    """
    url = make_url(url) if isinstance(url, str) else url
    if url.get_backend_name() == "sqlite":
        engine = create_engine(url, echo=echo, connect_args={"check_same_thread": False, **(connect_args or {})})
        _enable_sqlite_foreign_keys(engine)
        return engine
    if url.get_backend_name() == "postgresql":
        url, driver_args = _postgres_connect(url)
        return create_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=_env_int("CAMPUSNEXUS_DB_POOL_SIZE", 5, 1, 20),
            max_overflow=_env_int("CAMPUSNEXUS_DB_MAX_OVERFLOW", 5, 0, 20),
            pool_timeout=_env_int("CAMPUSNEXUS_DB_POOL_TIMEOUT", 30, 1, 120),
            pool_recycle=1800,
            connect_args={**driver_args, **(connect_args or {})},
        )
    raise DatabaseConfigError(f"Unsupported database '{url.get_backend_name()}'")


def create_db_engine(db_path: str | Path | None = None, *, echo: bool = False) -> Engine:
    """Create a new engine for ``db_path`` or the configured database (see ``get_database_url``)."""
    return create_database_engine(get_database_url(db_path), echo=echo)


def describe_database(bind: Engine | URL | str) -> dict[str, Any]:
    """A secret-free description of a database: dialect and a display label.

    Never includes the host, user, password or URL. ``supabase`` only says
    whether the host is a Supabase host.
    """
    url = bind.url if isinstance(bind, Engine) else (make_url(bind) if isinstance(bind, str) else bind)
    dialect = url.get_backend_name()
    if dialect == "postgresql":
        supabase = is_supabase_host(url.host)
        return {"dialect": dialect, "label": "PostgreSQL / Supabase" if supabase else "PostgreSQL", "supabase": supabase}
    return {"dialect": dialect, "label": "SQLite", "supabase": False}


def configured_postgres_url() -> str:
    """``CAMPUSNEXUS_DATABASE_URL`` as a PostgreSQL URL (its driver kept); refuses anything that is not PostgreSQL."""
    raw = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not raw:
        raise DatabaseConfigError(f"{DATABASE_URL_ENV} is not set")
    url = normalize_database_url(raw)
    if not url.startswith("postgresql+"):
        raise DatabaseConfigError(f"{DATABASE_URL_ENV} must name a PostgreSQL database (postgresql+psycopg://...)")
    return url


_ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){2,7}\b", re.IGNORECASE)


def safe_error(exc: BaseException, url: str | URL | None = None) -> str:
    """An exception message fit to print: the URL's password, user, host and any IP address removed.

    Driver errors such as psycopg's name the server and user ("connection to
    server at "<host>" (<ip>), port 5432 failed: ... for user "<user>"").
    """
    message = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    if url is not None:
        parsed = make_url(url) if isinstance(url, str) else url
        for secret in (parsed.render_as_string(hide_password=False), parsed.password, parsed.username, parsed.host):
            if secret:
                message = message.replace(str(secret), "***")
    return _ADDRESS.sub("***", message)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create all tables known to ``Base.metadata`` on ``engine``."""
    Base.metadata.create_all(engine)
    upgrade_schema(engine)


@dataclass
class SchemaPlan:
    """What ``upgrade_schema`` would change on a database (read-only)."""

    missing_tables: List[Table] = field(default_factory=list)
    missing_columns: List[Tuple[Table, Column]] = field(default_factory=list)
    missing_indexes: List[Index] = field(default_factory=list)
    # NOT NULL model columns an existing table lacks: an additive upgrade cannot add them (needs a manual migration).
    blocked_columns: List[str] = field(default_factory=list)

    @property
    def changes(self) -> List[str]:
        return ([t.name for t in self.missing_tables] + [f"{t.name}.{c.name}" for t, c in self.missing_columns]
                + [f"index:{i.name}" for i in self.missing_indexes])

    @property
    def current(self) -> bool:
        return not self.changes and not self.blocked_columns


def _plan(connection: Connection) -> SchemaPlan:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    plan = SchemaPlan(missing_tables=[t for t in Base.metadata.sorted_tables if t.name not in existing_tables])
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable or column.primary_key:
                plan.blocked_columns.append(f"{table.name}.{column.name}")
                continue
            plan.missing_columns.append((table, column))
            present.add(column.name)
        present_indexes = {index["name"] for index in inspector.get_indexes(table.name)}
        for index in sorted(table.indexes, key=lambda i: i.name):
            # A NOT NULL model column is never added to an old table, so neither is an index on it.
            if index.name not in present_indexes and set(index.columns.keys()) <= present:
                plan.missing_indexes.append(index)
    return plan


def plan_schema_upgrade(engine: Engine) -> SchemaPlan:
    """The additive changes ``upgrade_schema`` would make, without making them."""
    with engine.connect() as connection:
        return _plan(connection)


def upgrade_schema(engine: Engine) -> list[str]:
    """Add columns that exist in the models but not yet in an existing table.

    ``create_all`` never alters an existing table, so a database created before
    a phase added a column (e.g. Phase 11's approval binding columns) would fail
    on the first SELECT. Strictly additive and idempotent: it only ever adds
    *nullable* columns and never drops, renames or rewrites anything. Returns
    the ``table.column`` names it added. A table that did not exist yet (e.g.
    Phase 13's candidate/selection tables) is created, which is equally additive.

    Phase 22: an added column that references another table (``organization_id``)
    is added with its foreign key, and model indexes missing from an existing
    table are created (reported as ``index:<name>``). Still additive only.

    Phase 2.5: one transaction. On PostgreSQL (transactional DDL) a failure part
    way leaves the schema unchanged, and an advisory lock serializes concurrent
    upgraders. NOT NULL columns an old table lacks are never added (see
    ``plan_schema_upgrade().blocked_columns``).
    """
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _UPGRADE_LOCK_KEY})
        plan = _plan(connection)
        if plan.missing_tables:
            Base.metadata.create_all(connection, tables=plan.missing_tables)
        for table, column in plan.missing_columns:
            column_type = column.type.compile(dialect=connection.dialect)
            connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}{_references(table, column)}'))
        for index in plan.missing_indexes:
            index.create(connection)
        if connection.dialect.name == "postgresql":
            restrict_data_api_access(connection)
    return plan.changes


def _references(table: Table, column: Column) -> str:
    """The inline foreign key for a column added to an existing table (named per ``NAMING_CONVENTION``).

    Both SQLite and PostgreSQL accept ``ADD COLUMN ... CONSTRAINT name REFERENCES t(c)`` for a column that is
    empty in every existing row.
    """
    foreign_keys = list(column.foreign_keys)
    if len(foreign_keys) != 1:
        return ""
    target = foreign_keys[0].column
    name = f"fk_{table.name}_{column.name}_{target.table.name}"
    return f' CONSTRAINT "{name}" REFERENCES "{target.table.name}" ("{target.name}")'


@contextmanager
def _transaction(bind: Engine | Connection) -> Iterator[Connection]:
    if isinstance(bind, Connection):
        yield bind
    else:
        with bind.begin() as connection:
            yield connection


def restrict_data_api_access(bind: Engine | Connection) -> list[str]:
    """Keep application tables out of Supabase's Data API (Phase 21).

    The browser reaches data only through FastAPI; the ``anon``/``authenticated``
    roles that PostgREST uses must not read or write any application table.
    Where those roles exist, every application table gets row level security
    switched on (no policies, so those roles see nothing) and their privileges
    revoked. The application connects as the table owner, which RLS does not
    restrict. Idempotent: only tables not yet locked down are altered. Returns
    the tables it changed. A no-op on a server without those roles. Given a
    connection, it runs inside that connection's transaction.
    """
    with _transaction(bind) as connection:
        roles = set(connection.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"), {"names": list(SUPABASE_API_ROLES)}
        ).scalars())
        if not roles:
            return []
        changed: list[str] = []
        for table in Base.metadata.sorted_tables:
            row = connection.execute(text(
                "SELECT c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relname = :name AND c.relkind = 'r'"
            ), {"name": table.name}).first()
            if row is None:
                continue
            granted = connection.execute(text(
                "SELECT bool_or(has_table_privilege(r, format('%I', CAST(:name AS text)), p)) "
                "FROM unnest(CAST(:roles AS text[])) AS r, unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) AS p"
            ), {"roles": sorted(roles), "name": table.name}).scalar()
            if row[0] and not granted:
                continue
            if not row[0]:
                connection.execute(text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY'))
            connection.execute(text(f'REVOKE ALL ON TABLE "{table.name}" FROM {", ".join(sorted(roles))}'))
            changed.append(table.name)
    return changed


def open_database(db_path: str | Path | None = None, *, echo: bool = False) -> Engine:
    """Engine for an executable entry point that expects the current schema (Phase 17).

    Creates the engine and runs the idempotent, additive ``upgrade_schema`` once,
    so a database created by an earlier phase gains the new tables and nullable
    columns before any model touches them. Call it from ``main()``/runtime code,
    never at module import time.
    """
    engine = create_db_engine(db_path, echo=echo)
    upgrade_schema(engine)
    return engine


def verify_database_ready(engine: Engine) -> SchemaPlan:
    """Connect and confirm the schema is current; raise ``DatabaseUnavailableError`` (secret-free) otherwise.

    Used at API startup for PostgreSQL instead of upgrading it: production schema
    changes are applied deliberately (``scripts/upgrade_database.py``), never as a
    side effect of starting a server.
    """
    try:
        plan = plan_schema_upgrade(engine)
    except Exception as exc:  # noqa: BLE001 -- driver errors can name the host/user
        raise DatabaseUnavailableError(f"database unreachable ({safe_error(exc, engine.url)})") from None
    if not plan.current:
        pending = plan.changes + [f"{name} (NOT NULL, manual migration)" for name in plan.blocked_columns]
        shown = ", ".join(pending[:8]) + (f", ... {len(pending) - 8} more" if len(pending) > 8 else "")
        raise DatabaseUnavailableError(
            f"database schema is not current ({shown}); run: python scripts/upgrade_database.py")
    return plan


def drop_db(engine: Engine) -> None:
    """Drop all tables known to ``Base.metadata`` on ``engine`` (tests only)."""
    Base.metadata.drop_all(engine)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional session: commits on success, rolls back on error."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
