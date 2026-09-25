"""Declarative base, naming convention, and shared column types.

Datetime strategy (see docs/ARCHITECTURE.md): every instant stored in the
database is UTC. ``UTCDateTime`` requires timezone-aware input on the way in
(stored as naive UTC on SQLite, as ``timestamp with time zone`` on PostgreSQL)
and re-attaches ``timezone.utc`` on the way out, so application code only ever compares timezone-aware UTC
datetimes -- naive datetime comparisons are a programming error, not a
possibility.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator, TypeEngine

# A fixed naming convention keeps auto-generated constraint/index names
# stable and predictable, which matters the moment a migration tool (e.g.
# Alembic) is introduced in a later phase.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base for all CampusNexus ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UTCDateTime(TypeDecorator):
    """A DateTime column that always round-trips as timezone-aware UTC.

    SQLite has no native timezone-aware storage, so values are normalized to
    naive UTC before binding and re-tagged with ``timezone.utc`` after
    loading. On PostgreSQL the column is ``timestamp with time zone`` and the
    aware UTC value is bound as-is; whatever session time zone the server uses,
    the value read back is converted to UTC. Naive input is rejected on every
    dialect -- callers must be explicit about timezone, matching CLAUDE.md's
    timezone-aware datetime rule.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime requires a timezone-aware datetime")
        value = value.astimezone(timezone.utc)
        return value if dialect.name == "postgresql" else value.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# Longest enum value any column may hold. Enum columns are plain VARCHARs, so a
# value added to a Python enum later never needs a database migration.
ENUM_VALUE_LENGTH = 64


def portable_enum(enum_cls: type[enum.Enum]) -> SAEnum:
    """An Enum column stored as its ``.value`` in a VARCHAR on every dialect (Phase 21).

    ``native_enum=False`` keeps PostgreSQL from creating a ``CREATE TYPE`` per
    enum: a native type would need ``ALTER TYPE`` whenever a phase adds a member,
    which the additive ``upgrade_schema`` cannot do, so a new status would work
    on SQLite and fail on PostgreSQL. No CHECK constraint, matching SQLite.
    """
    return SAEnum(
        enum_cls, values_callable=lambda e: [m.value for m in e],
        native_enum=False, create_constraint=False, length=ENUM_VALUE_LENGTH,
    )


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)
