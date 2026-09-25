"""Optional PostgreSQL test support (Phase 21).

The normal suite is SQLite-only and offline. PostgreSQL tests run only when
``CAMPUSNEXUS_TEST_DATABASE_URL`` names a PostgreSQL database; otherwise they
skip. Each test gets its own throwaway schema (dropped afterwards), so tests
never see each other's rows and never touch the ``public`` schema.

``CAMPUSNEXUS_TEST_ALL_ON_POSTGRES=1`` additionally points the shared ``engine``
fixture at such a schema, so every fixture-based test runs on PostgreSQL (a
portability audit; tests that build their own SQLite engine stay on SQLite).
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

import pytest
from sqlalchemy import Engine, text

from app.db.session import create_database_engine, normalize_database_url

TEST_DATABASE_URL_ENV = "CAMPUSNEXUS_TEST_DATABASE_URL"
ALL_ON_POSTGRES_ENV = "CAMPUSNEXUS_TEST_ALL_ON_POSTGRES"


def postgres_test_url() -> Optional[str]:
    raw = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not raw:
        return None
    url = normalize_database_url(raw)
    return url if url.startswith("postgresql") else None


def all_on_postgres() -> bool:
    return os.environ.get(ALL_ON_POSTGRES_ENV) == "1" and postgres_test_url() is not None


# Connections still checked out when a schema engine was torn down, per schema.
LEAKED_CONNECTIONS: dict[str, int] = {}

requires_postgres = pytest.mark.skipif(
    postgres_test_url() is None, reason=f"{TEST_DATABASE_URL_ENV} is not set to a PostgreSQL URL"
)


@contextmanager
def postgres_schema_engine(url: Optional[str] = None) -> Iterator[Engine]:
    """An engine whose search_path is a fresh schema; the schema is dropped on exit."""
    url = url or postgres_test_url()
    assert url, f"{TEST_DATABASE_URL_ENV} is not set"
    schema = f"cn_test_{uuid.uuid4().hex[:12]}"
    admin = create_database_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_database_engine(url, connect_args={"options": f"-csearch_path={schema}", "application_name": schema})
    try:
        yield engine
    finally:
        LEAKED_CONNECTIONS[schema] = engine.pool.checkedout()
        engine.dispose()
        with admin.begin() as connection:
            # A connection still checked out (a session never closed) would block the drop forever.
            connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = :name AND pid <> pg_backend_pid()"),
                {"name": schema},
            )
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
