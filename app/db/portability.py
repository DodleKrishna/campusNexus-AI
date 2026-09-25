"""Copy and verify CampusNexus data between databases (Phase 21).

Used by ``scripts/migrate_sqlite_to_postgres.py`` and the PostgreSQL tests. It
works on the SQLAlchemy metadata (``Base.metadata``), never on hand-written
per-table SQL, so a table added by a later phase is covered automatically.

Rules:
- rows are copied through the model column types, so timestamps stay aware
  UTC, JSON stays JSON and enum values are validated on the way;
- tables are copied parent-first (``Base.metadata.sorted_tables``) and rows in
  primary-key order, inside one target transaction. A foreign key that points
  forward (a cycle such as departments.hod_faculty_id <-> faculty_profiles, or
  a self reference) is inserted as NULL and set once every row exists;
- primary keys are preserved and PostgreSQL sequences are moved past them;
- a populated target is never overwritten: by default every target table must
  be empty, and ``merge_identical`` only adds rows whose primary key is
  missing, refusing if any existing row differs or is not in the source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import Engine, String, Table, and_, bindparam, exists, func, inspect, not_, select, text
from sqlalchemy.engine import Connection

from app.db import models  # noqa: F401  (registers every mapped table)
from app.db.base import Base

BATCH_SIZE = 500


class MigrationError(RuntimeError):
    """The copy cannot proceed safely; nothing was written."""


@dataclass
class TableCopy:
    table: str
    source_rows: int
    target_rows_before: int = 0
    inserted: int = 0


@dataclass
class CopyReport:
    tables: List[TableCopy] = field(default_factory=list)
    ignored_source_tables: List[str] = field(default_factory=list)
    sequences_reset: List[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def inserted(self) -> int:
        return sum(t.inserted for t in self.tables)


@dataclass(frozen=True)
class CountCheck:
    table: str
    source: int
    target: int

    @property
    def ok(self) -> bool:
        return self.source == self.target


@dataclass(frozen=True)
class Orphans:
    table: str
    columns: Tuple[str, ...]
    parent: str
    rows: int


def application_tables() -> List[Table]:
    """Every application table, parents before children."""
    return list(Base.metadata.sorted_tables)


def _existing_tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def table_counts(engine: Engine, tables: Optional[Sequence[Table]] = None) -> Dict[str, int]:
    """Row count per application table; a table missing from the database counts 0."""
    present = _existing_tables(engine)
    counts: Dict[str, int] = {}
    with engine.connect() as connection:
        for table in tables or application_tables():
            counts[table.name] = (
                connection.execute(select(func.count()).select_from(table)).scalar_one() if table.name in present else 0
            )
    return counts


def compare_counts(source: Engine, target: Engine) -> List[CountCheck]:
    source_counts, target_counts = table_counts(source), table_counts(target)
    return [CountCheck(name, source_counts[name], target_counts[name]) for name in source_counts]


def _source_columns(engine: Engine, table: Table) -> List[Any]:
    present = {column["name"] for column in inspect(engine).get_columns(table.name)}
    return [column for column in table.columns if column.name in present]


def _pk_key(table: Table, row: Dict[str, Any]) -> Tuple[Any, ...]:
    return tuple(row[column.name] for column in table.primary_key.columns)


def read_rows(engine: Engine, table: Table) -> List[Dict[str, Any]]:
    """All rows of ``table`` in primary-key order, decoded through the model types.

    Columns the source does not have yet (added by a later phase) are left out,
    so they take their nullable default in the target. An enum value the
    application no longer knows raises here, before anything is written.
    """
    columns = _source_columns(engine, table)
    order = list(table.primary_key.columns)
    with engine.connect() as connection:
        result = connection.execute(select(*columns).order_by(*order))
        return [dict(row._mapping) for row in result]


def length_violations(engine: Engine, tables: Optional[Sequence[Table]] = None) -> List[str]:
    """``table.column (max N > limit)`` for every VARCHAR value longer than its declared length.

    SQLite ignores VARCHAR lengths; PostgreSQL enforces them, so such a row
    would abort the copy. Checked up front so a dry run reports it.
    """
    present = _existing_tables(engine)
    problems: List[str] = []
    with engine.connect() as connection:
        for table in tables or application_tables():
            if table.name not in present:
                continue
            for column in _source_columns(engine, table):
                limit = getattr(column.type, "length", None)
                if not isinstance(column.type, String) or not limit:
                    continue
                longest = connection.execute(select(func.max(func.length(column)))).scalar()
                if longest is not None and longest > limit:
                    problems.append(f"{table.name}.{column.name} (max {longest} > {limit})")
    return problems


def find_orphans(engine: Engine, tables: Optional[Sequence[Table]] = None) -> List[Orphans]:
    """Rows whose non-null foreign key points at no parent row, for every declared FK."""
    present = _existing_tables(engine)
    found: List[Orphans] = []
    with engine.connect() as connection:
        for table in tables or application_tables():
            if table.name not in present:
                continue
            for constraint in table.foreign_key_constraints:
                parent = constraint.referred_table
                if parent.name not in present:
                    continue
                pairs = [(element.parent, element.column) for element in constraint.elements]
                child = table.alias("child")
                referred = parent.alias("parent")
                matched = exists().where(and_(*(referred.c[p.name] == child.c[c.name] for c, p in pairs)))
                not_null = and_(*(child.c[c.name].isnot(None) for c, _ in pairs))
                rows = connection.execute(
                    select(func.count()).select_from(child).where(not_null, not_(matched))
                ).scalar_one()
                if rows:
                    found.append(Orphans(table.name, tuple(c.name for c, _ in pairs), parent.name, rows))
    return found


def declared_foreign_keys(engine: Engine) -> Dict[str, int]:
    """Foreign-key constraints the database actually holds, per application table."""
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    return {t.name: len(inspector.get_foreign_keys(t.name)) for t in application_tables() if t.name in present}


def missing_foreign_keys(engine: Engine) -> List[str]:
    """Application tables with fewer database FK constraints than the models declare."""
    declared = declared_foreign_keys(engine)
    return [
        f"{t.name} ({declared.get(t.name, 0)} of {len(t.foreign_key_constraints)})"
        for t in application_tables()
        if declared.get(t.name, 0) < len(t.foreign_key_constraints)
    ]


def reset_sequences(connection: Connection) -> List[str]:
    """Move each PostgreSQL id sequence past the highest copied id (explicit ids do not advance it)."""
    if connection.dialect.name != "postgresql":
        return []
    reset: List[str] = []
    for table in application_tables():
        column = table.autoincrement_column
        if column is None:
            continue
        sequence = connection.execute(
            text("SELECT pg_get_serial_sequence(:table, :column)"), {"table": table.name, "column": column.name}
        ).scalar()
        if not sequence:
            continue
        connection.execute(
            text(f'SELECT setval(CAST(:seq AS regclass), COALESCE((SELECT MAX("{column.name}") FROM "{table.name}"), 0) + 1, false)'),
            {"seq": sequence},
        )
        reset.append(table.name)
    return reset


def _plan(source: Engine, target: Engine, *, merge_identical: bool) -> Tuple[List[Tuple[Table, List[Dict[str, Any]]]], CopyReport]:
    source_tables = _existing_tables(source)
    target_tables = _existing_tables(target)
    known = {t.name for t in application_tables()}
    report = CopyReport(ignored_source_tables=sorted(source_tables - known))
    plan: List[Tuple[Table, List[Dict[str, Any]]]] = []
    conflicts: List[str] = []
    for table in application_tables():
        rows = read_rows(source, table) if table.name in source_tables else []
        existing = read_rows(target, table) if table.name in target_tables else []
        entry = TableCopy(table=table.name, source_rows=len(rows), target_rows_before=len(existing))
        if existing and not merge_identical:
            conflicts.append(f"{table.name} already has {len(existing)} rows")
        pending = rows
        if existing and merge_identical:
            by_key = {_pk_key(table, row): row for row in rows}
            for row in existing:
                source_row = by_key.get(_pk_key(table, row))
                if source_row is None:
                    conflicts.append(f"{table.name} id {_pk_key(table, row)} is not in the source")
                elif any(row.get(name) != value for name, value in source_row.items()):
                    conflicts.append(f"{table.name} id {_pk_key(table, row)} differs from the source")
            present_keys = {_pk_key(table, row) for row in existing}
            pending = [row for row in rows if _pk_key(table, row) not in present_keys]
        entry.inserted = len(pending)
        report.tables.append(entry)
        plan.append((table, pending))
    if conflicts:
        shown = "; ".join(conflicts[:10]) + (f"; ... {len(conflicts) - 10} more" if len(conflicts) > 10 else "")
        hint = "" if merge_identical else " Use an empty database, or --merge-identical to add only missing rows."
        raise MigrationError(f"The target database already holds data: {shown}.{hint}")
    return plan, report


def copy_database(source: Engine, target: Engine, *, dry_run: bool = False, merge_identical: bool = False) -> CopyReport:
    """Copy every application row from ``source`` into ``target`` (whose schema must exist).

    Refuses (``MigrationError``, nothing written) when a value would not fit
    PostgreSQL, when the source has orphan rows, or when the target already
    holds data it would conflict with. The whole copy is one transaction.
    """
    missing = [t.name for t in application_tables() if t.name not in _existing_tables(target)]
    if missing and not dry_run:
        raise MigrationError(f"Target schema is missing tables ({', '.join(missing[:5])}...); initialize it first.")
    too_long = length_violations(source)
    if too_long:
        raise MigrationError("Values longer than their column allows (PostgreSQL enforces lengths): " + "; ".join(too_long))
    orphans = find_orphans(source)
    if orphans:
        raise MigrationError("The source has orphan rows: " + "; ".join(
            f"{o.table}.{','.join(o.columns)} -> {o.parent}: {o.rows}" for o in orphans))
    plan, report = _plan(source, target, merge_identical=merge_identical)
    report.dry_run = dry_run
    if dry_run:
        return report
    with target.begin() as connection:
        deferred_updates = []
        for table, rows in plan:
            forward = deferred_columns(table)
            if forward:
                later = [row for row in rows if any(row.get(name) is not None for name in forward)]
                if later:
                    deferred_updates.append((table, forward, later))
                rows = [{**row, **{name: None for name in forward if name in row}} for row in rows]
            for start in range(0, len(rows), BATCH_SIZE):
                connection.execute(table.insert(), rows[start:start + BATCH_SIZE])
        for table, forward, rows in deferred_updates:
            _apply_deferred(connection, table, forward, rows)
        report.sequences_reset = reset_sequences(connection)
    return report


def deferred_columns(table: Table) -> List[str]:
    """FK columns referring to this table itself or to a table copied after it."""
    order = {t.name: index for index, t in enumerate(application_tables())}
    names: List[str] = []
    for constraint in table.foreign_key_constraints:
        if order.get(constraint.referred_table.name, -1) >= order[table.name]:
            names.extend(element.parent.name for element in constraint.elements)
    return names


def _apply_deferred(connection: Connection, table: Table, forward: List[str], rows: List[Dict[str, Any]]) -> None:
    keys = list(table.primary_key.columns)
    present = [name for name in forward if name in rows[0]]
    statement = table.update().where(*(key == bindparam(f"pk_{key.name}") for key in keys)).values(
        {name: bindparam(f"v_{name}") for name in present}
    )
    params = [
        {**{f"pk_{key.name}": row[key.name] for key in keys}, **{f"v_{name}": row[name] for name in present}}
        for row in rows
    ]
    for start in range(0, len(params), BATCH_SIZE):
        connection.execute(statement, params[start:start + BATCH_SIZE])


def summarize_counts(checks: Iterable[CountCheck]) -> List[str]:
    lines = [f"{'table':<32}{'source':>8}{'target':>8}  status"]
    for check in checks:
        lines.append(f"{check.table:<32}{check.source:>8}{check.target:>8}  {'OK' if check.ok else 'MISMATCH'}")
    return lines
