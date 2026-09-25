"""Phase 22B: the unauthenticated readiness probe's database check.

``GET /health`` runs before anyone signs in, so it has no organization and its
tenant session cannot read tenant tables. This probe runs directly on the
engine and returns only connectivity and one aggregate number (how many
student rows exist in the whole database, i.e. "is it seeded") -- never a row,
a name or anything organization-specific.
"""
from __future__ import annotations

from typing import Tuple

from sqlalchemy import Engine, func, select, text

from app.db.models.identity import Student


def database_readiness(engine: Engine) -> Tuple[bool, int]:
    """(reachable, total student rows). Raises on a connection error; the caller reports it without the URL."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        students = connection.execute(select(func.count()).select_from(Student.__table__)).scalar_one()
    return True, students
