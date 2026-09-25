"""Phase 21 -- the PostgreSQL test-schema isolation guard (tests/pg_support.py), offline.

The guard is what keeps PostgreSQL tests out of a real database's ``public``
schema, so it is tested without a database: schema names are validated before
they reach SQL, and a connection that does not land in its throwaway schema is
refused before anything can run on it.
"""
from __future__ import annotations

import pytest

from tests.pg_support import (
    SchemaIsolationError,
    _isolate_connection,
    assert_isolated,
    new_test_schema_name,
    validate_test_schema_name,
)

SCHEMA = "cn_test_0123456789ab"


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.connection.executed.append((sql, self.connection.autocommit))

    def fetchone(self) -> tuple:
        return (self.connection.reported_schema,)


class _FakeConnection:
    """A DBAPI connection whose server reports ``reported_schema`` as current_schema()."""

    def __init__(self, reported_schema: object) -> None:
        self.reported_schema = reported_schema
        self.autocommit = False
        self.executed: list[tuple[str, bool]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def test_generated_names_follow_the_cn_test_convention() -> None:
    first, second = new_test_schema_name(), new_test_schema_name()
    assert first != second
    assert validate_test_schema_name(first) == first and first.startswith("cn_test_")


@pytest.mark.parametrize("name", [
    "public", "PUBLIC", "", "cn_test_", "cn_test_0123456789a", "cn_test_0123456789abc", "cn_test_0123456789AB",
    'cn_test_0123456789ab"; DROP SCHEMA public CASCADE; --', "cn_test_0123456789ab\n", "x_cn_test_0123456789ab",
    "cn_test_../public", None,
])
def test_anything_but_a_generated_name_is_refused(name) -> None:
    with pytest.raises(SchemaIsolationError):
        validate_test_schema_name(name)


@pytest.mark.parametrize("actual", ["public", None, "cn_test_ffffffffffff", "other"])
def test_a_connection_outside_its_schema_is_refused(actual) -> None:
    with pytest.raises(SchemaIsolationError):
        assert_isolated(actual, SCHEMA)


def test_a_connection_in_its_schema_is_accepted() -> None:
    assert_isolated(SCHEMA, SCHEMA)


def test_new_connections_set_the_schema_in_autocommit_and_verify_it() -> None:
    connection = _FakeConnection(SCHEMA)
    _isolate_connection(connection, SCHEMA)
    statements = [sql for sql, _ in connection.executed]
    assert statements[0] == f'SET search_path TO "{SCHEMA}"' and statements[-1] == "SELECT current_schema()"
    assert all(autocommit for _, autocommit in connection.executed)  # a later rollback cannot undo the SET
    assert connection.autocommit is False  # restored for the application


def test_a_connection_that_stays_on_public_aborts_before_anything_else_runs() -> None:
    # What Supabase's session pooler did with the old startup option: the connection stayed on public.
    connection = _FakeConnection("public")
    with pytest.raises(SchemaIsolationError):
        _isolate_connection(connection, SCHEMA)
    assert not any("CREATE" in sql or "INSERT" in sql or "DROP" in sql for sql, _ in connection.executed)


def test_an_unvalidated_schema_never_reaches_sql() -> None:
    connection = _FakeConnection("public")
    with pytest.raises(SchemaIsolationError):
        _isolate_connection(connection, "public")
    assert connection.executed == []
