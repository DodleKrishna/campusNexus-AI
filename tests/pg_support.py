"""Optional PostgreSQL test support (Phase 21).

The normal suite is SQLite-only and offline. PostgreSQL tests run only when
``CAMPUSNEXUS_TEST_DATABASE_URL`` names a PostgreSQL database; otherwise they
skip. Each test gets its own throwaway schema (dropped afterwards), so tests
never see each other's rows and never touch the ``public`` schema.

The schema is selected with ``SET search_path`` on every new DBAPI connection,
not with the ``options=-csearch_path=...`` startup parameter: Supabase's
session pooler silently ignores startup options, which would leave the tests in
``public``. Each new connection is then checked (``current_schema()`` must be the
test schema, never ``public``) before anything can run on it, and the engine is
checked once more before it is handed to a fixture (so ``create_all``, seeding
and migrations never start on the wrong schema). Only names matching
``cn_test_<12 hex>`` are ever interpolated into SQL or dropped.

``CAMPUSNEXUS_TEST_ALL_ON_POSTGRES=1`` additionally points the shared ``engine``
fixture at such a schema, so every fixture-based test runs on PostgreSQL (a
portability audit; tests that build their own SQLite engine stay on SQLite).
"""
from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import pytest
from sqlalchemy import Engine, event, text

from app.db.session import create_database_engine, normalize_database_url

TEST_DATABASE_URL_ENV = "CAMPUSNEXUS_TEST_DATABASE_URL"
ALL_ON_POSTGRES_ENV = "CAMPUSNEXUS_TEST_ALL_ON_POSTGRES"
TEST_SCHEMA_PREFIX = "cn_test_"
_TEST_SCHEMA_NAME = re.compile(r"cn_test_[0-9a-f]{12}")


class SchemaIsolationError(AssertionError):
    """A test connection is not confined to its throwaway schema."""


def postgres_test_url() -> Optional[str]:
    raw = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not raw:
        return None
    url = normalize_database_url(raw)
    return url if url.startswith("postgresql") else None


def all_on_postgres() -> bool:
    return os.environ.get(ALL_ON_POSTGRES_ENV) == "1" and postgres_test_url() is not None


def new_test_schema_name() -> str:
    return validate_test_schema_name(f"{TEST_SCHEMA_PREFIX}{uuid.uuid4().hex[:12]}")


def validate_test_schema_name(name: str) -> str:
    """Return ``name`` if it is a generated throwaway schema name; raise otherwise."""
    if not isinstance(name, str) or not _TEST_SCHEMA_NAME.fullmatch(name):
        raise SchemaIsolationError(f"refusing schema name {name!r}: only generated cn_test_<12 hex> names are allowed")
    return name


def assert_isolated(actual: Any, expected: str) -> None:
    """``current_schema()`` of a test connection must be exactly its throwaway schema."""
    expected = validate_test_schema_name(expected)
    if actual is None or actual == "public" or actual != expected:
        raise SchemaIsolationError(f"test connection is on schema {actual!r}, expected {expected!r}; aborting before any write")


def _isolate_connection(dbapi_connection: Any, schema: str) -> None:
    """Point a new DBAPI connection at ``schema`` and verify it, committed so a rollback cannot undo it."""
    schema = validate_test_schema_name(schema)
    previous = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    try:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute(f"SET application_name TO '{schema}'")
            cursor.execute("SELECT current_schema()")
            assert_isolated(cursor.fetchone()[0], schema)
    finally:
        dbapi_connection.autocommit = previous


def _reset_connection(dbapi_connection: Any) -> None:
    """Best effort before a connection closes: a session pooler may hand the server session to another client."""
    try:
        dbapi_connection.rollback()
        dbapi_connection.autocommit = True
        with dbapi_connection.cursor() as cursor:
            cursor.execute("RESET search_path")
            cursor.execute("RESET application_name")
    except Exception:  # noqa: BLE001 -- the connection may already be broken; closing it is enough
        pass


def isolate_engine(engine: Engine, schema: str) -> None:
    """Every connection ``engine`` opens is confined to ``schema`` and reset before it closes."""
    schema = validate_test_schema_name(schema)
    event.listen(engine, "connect", lambda dbapi_connection, _record: _isolate_connection(dbapi_connection, schema))
    event.listen(engine, "close", lambda dbapi_connection, _record: _reset_connection(dbapi_connection))


# Connections still checked out when a schema engine was torn down, per schema.
LEAKED_CONNECTIONS: dict[str, int] = {}

requires_postgres = pytest.mark.skipif(
    postgres_test_url() is None, reason=f"{TEST_DATABASE_URL_ENV} is not set to a PostgreSQL URL"
)


@contextmanager
def postgres_schema_engine(url: Optional[str] = None) -> Iterator[Engine]:
    """An engine confined to a fresh ``cn_test_*`` schema; the schema is dropped on exit."""
    url = url or postgres_test_url()
    assert url, f"{TEST_DATABASE_URL_ENV} is not set"
    schema = new_test_schema_name()
    admin = create_database_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_database_engine(url, connect_args={"application_name": schema})
    isolate_engine(engine, schema)
    try:
        with engine.connect() as connection:  # fails here, before any fixture creates or writes anything
            assert_isolated(connection.execute(text("SELECT current_schema()")).scalar(), schema)
        yield engine
    finally:
        LEAKED_CONNECTIONS[schema] = engine.pool.checkedout()
        engine.dispose()
        _drop_test_schema(admin, schema)
        admin.dispose()


def _drop_test_schema(admin: Engine, schema: str) -> None:
    schema = validate_test_schema_name(schema)
    with admin.begin() as connection:
        # A connection still checked out (a session never closed) would block the drop forever.
        connection.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = :name AND pid <> pg_backend_pid()"),
            {"name": schema},
        )
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
