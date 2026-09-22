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

from sqlalchemy import Engine, create_engine, event
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
