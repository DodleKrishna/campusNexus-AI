"""Shared fixtures for the database/context-service/seed-data test suite.

Every test gets its own throwaway SQLite file under pytest's ``tmp_path`` and
its own engine/session, and ``CAMPUSNEXUS_DB_PATH`` is monkeypatched to that
path -- the development database under data/campusnexus.db is never opened by
the test suite.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import create_db_engine, create_session_factory, init_db
from scripts.seed_data import run_seed


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    db_file = tmp_path / "test_campusnexus.db"
    monkeypatch.setenv("CAMPUSNEXUS_DB_PATH", str(db_file))
    eng = create_db_engine(db_path=str(db_file))
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture()
def session(session_factory: sessionmaker[Session]):
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def seeded_session(session_factory: sessionmaker[Session]):
    """A session against a freshly-seeded (once) test database."""
    with session_factory() as setup_session:
        run_seed(setup_session)

    s = session_factory()
    try:
        yield s
    finally:
        s.close()
