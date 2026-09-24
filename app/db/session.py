"""Engine/session creation and database initialization.

No module-level global engine or session is kept here -- every caller
(scripts/seed_data.py, the Context Service, tests) explicitly creates its own
engine and session factory. This keeps tests fully isolated from the
development database and from each other: a test that wants a throwaway
database just points ``CAMPUSNEXUS_DB_PATH`` (or the ``db_path`` argument) at
a temp file or ``:memory:`` before building its own engine.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base

# Project root: .../app/db/session.py -> parents[2] == repo root.
BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = BASE_DIR / "data" / "campusnexus.db"


def get_database_url(db_path: str | Path | None = None) -> str:
    """Resolve the SQLite URL to use.

    Resolution order: explicit ``db_path`` argument, then the
    ``CAMPUSNEXUS_DB_PATH`` environment variable, then the repo-relative
    default (``data/campusnexus.db``). ``:memory:`` is passed through as an
    in-memory database.
    """
    raw = db_path if db_path is not None else os.environ.get("CAMPUSNEXUS_DB_PATH")
    if raw is None:
        raw = DEFAULT_DB_PATH

    raw = str(raw)
    if raw == ":memory:":
        return "sqlite:///:memory:"

    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite ignores FOREIGN KEY constraints unless enabled per-connection."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_db_engine(db_path: str | Path | None = None, *, echo: bool = False) -> Engine:
    """Create a new engine for ``db_path`` (see ``get_database_url``)."""
    url = get_database_url(db_path)
    connect_args = {"check_same_thread": False}
    engine = create_engine(url, echo=echo, connect_args=connect_args)
    _enable_sqlite_foreign_keys(engine)
    return engine


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
                connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}'))
                added.append(f"{table.name}.{column.name}")
    return added


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
