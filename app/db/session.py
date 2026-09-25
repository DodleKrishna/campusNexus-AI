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
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Column, Engine, Table, create_engine, event, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base

# Project root: .../app/db/session.py -> parents[2] == repo root.
BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = BASE_DIR / "data" / "campusnexus.db"

DATABASE_URL_ENV = "CAMPUSNEXUS_DATABASE_URL"
DB_PATH_ENV = "CAMPUSNEXUS_DB_PATH"
# The only supported PostgreSQL driver (psycopg 3, the "postgres" extra).
POSTGRES_DRIVER = "postgresql+psycopg"
# Roles through which Supabase's Data API (PostgREST) reaches the database.
SUPABASE_API_ROLES = ("anon", "authenticated")


class DatabaseConfigError(ValueError):
    """``CAMPUSNEXUS_DATABASE_URL`` names an unsupported database or driver."""


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

    ``postgresql://``/``postgres://`` (the form Supabase shows) are routed to the
    psycopg 3 driver. Any other dialect or driver is refused rather than guessed.
    """
    try:
        url = make_url(raw.strip())
    except Exception as exc:  # noqa: BLE001 -- never echo the value, it may hold a password
        raise DatabaseConfigError(f"{DATABASE_URL_ENV} is not a valid database URL") from exc
    backend, _, driver = url.drivername.partition("+")
    if backend in ("postgresql", "postgres"):
        if driver not in ("", "psycopg"):
            raise DatabaseConfigError(f"Unsupported PostgreSQL driver '{driver}'; use {POSTGRES_DRIVER}://")
        return url.set(drivername=POSTGRES_DRIVER).render_as_string(hide_password=False)
    if backend == "sqlite":
        if url.database in (None, "", ":memory:"):
            return "sqlite:///:memory:"
        return _sqlite_url(url.database)
    raise DatabaseConfigError(f"Unsupported database '{backend}'; use sqlite:/// or postgresql+psycopg://")


def get_database_url(db_path: str | Path | None = None) -> str:
    """Resolve the database URL to use.

    Resolution order: an explicit ``db_path`` argument (always SQLite -- tests,
    evals and the local demo reset pin their own file this way), then
    ``CAMPUSNEXUS_DATABASE_URL`` (PostgreSQL or SQLite), then
    ``CAMPUSNEXUS_DB_PATH``, then the repo-relative default
    (``data/campusnexus.db``). ``:memory:`` is passed through as an in-memory
    SQLite database.
    """
    if db_path is not None:
        return _sqlite_url(db_path)
    configured = os.environ.get(DATABASE_URL_ENV, "").strip()
    if configured:
        return normalize_database_url(configured)
    return _sqlite_url(os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite ignores FOREIGN KEY constraints unless enabled per-connection."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def create_database_engine(url: str | URL, *, echo: bool = False, connect_args: dict[str, Any] | None = None) -> Engine:
    """The one dialect-aware engine factory. Never connects by itself.

    SQLite: ``check_same_thread=False`` (FastAPI's threadpool shares the engine)
    and foreign keys switched on per connection.

    PostgreSQL (psycopg 3): a small pre-pinged pool sized for Supabase's Session
    pooler, recycled before idle connections are dropped. SSL and the host come
    from the URL (e.g. ``?sslmode=require``); nothing Supabase-specific is
    hardcoded. Server-side prepared statements are disabled so the same URL
    also works behind a transaction-mode pooler.
    """
    url = make_url(url) if isinstance(url, str) else url
    if url.get_backend_name() == "sqlite":
        engine = create_engine(url, echo=echo, connect_args={"check_same_thread": False, **(connect_args or {})})
        _enable_sqlite_foreign_keys(engine)
        return engine
    if url.get_backend_name() == "postgresql":
        return create_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=_env_int("CAMPUSNEXUS_DB_POOL_SIZE", 5),
            max_overflow=_env_int("CAMPUSNEXUS_DB_MAX_OVERFLOW", 5),
            pool_timeout=30,
            pool_recycle=1800,
            connect_args={"connect_timeout": 15, "application_name": "campusnexus", "prepare_threshold": None, **(connect_args or {})},
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
        supabase = (url.host or "").lower().endswith((".supabase.com", ".supabase.co"))
        return {"dialect": dialect, "label": "PostgreSQL / Supabase" if supabase else "PostgreSQL", "supabase": supabase}
    return {"dialect": dialect, "label": "SQLite", "supabase": False}


def configured_postgres_url() -> str:
    """``CAMPUSNEXUS_DATABASE_URL`` as a psycopg URL; refuses anything that is not PostgreSQL."""
    raw = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not raw:
        raise DatabaseConfigError(f"{DATABASE_URL_ENV} is not set")
    url = normalize_database_url(raw)
    if not url.startswith(POSTGRES_DRIVER):
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
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    missing = [table for table in Base.metadata.sorted_tables if table.name not in existing_tables]
    if missing:
        Base.metadata.create_all(engine, tables=missing)
        added.extend(table.name for table in missing)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present or not column.nullable or column.primary_key:
                    continue
                column_type = column.type.compile(dialect=engine.dialect)
                connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}{_references(table, column)}'))
                added.append(f"{table.name}.{column.name}")
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            live = inspect(connection)
            present_indexes = {index["name"] for index in live.get_indexes(table.name)}
            present_columns = {column["name"] for column in live.get_columns(table.name)}
            for index in sorted(table.indexes, key=lambda i: i.name):
                # A NOT NULL model column is never added to an old table, so neither is an index on it.
                if index.name not in present_indexes and set(index.columns.keys()) <= present_columns:
                    index.create(connection)
                    added.append(f"index:{index.name}")
    if engine.dialect.name == "postgresql":
        restrict_data_api_access(engine)
    return added


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


def restrict_data_api_access(engine: Engine) -> list[str]:
    """Keep application tables out of Supabase's Data API (Phase 21).

    The browser reaches data only through FastAPI; the ``anon``/``authenticated``
    roles that PostgREST uses must not read or write any application table.
    Where those roles exist, every application table gets row level security
    switched on (no policies, so those roles see nothing) and their privileges
    revoked. The application connects as the table owner, which RLS does not
    restrict. Idempotent: only tables not yet locked down are altered. Returns
    the tables it changed. A no-op on a server without those roles.
    """
    with engine.begin() as connection:
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
