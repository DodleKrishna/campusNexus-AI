"""Declarative base, naming convention, and shared column types.

Datetime strategy (see docs/ARCHITECTURE.md): every instant stored in the
database is UTC. ``UTCDateTime`` requires timezone-aware input on the way in
(converted to naive UTC for SQLite storage) and re-attaches ``timezone.utc``
on the way out, so application code only ever compares timezone-aware UTC
datetimes -- naive datetime comparisons are a programming error, not a
possibility.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

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
    loading. Naive input is rejected -- callers must be explicit about
    timezone, matching CLAUDE.md's timezone-aware datetime rule.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime requires a timezone-aware datetime")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)
