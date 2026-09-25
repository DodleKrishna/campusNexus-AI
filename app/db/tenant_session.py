"""Phase 22B: organization-bound database sessions -- the runtime database path.

Every API request, agent, tool and orchestrator step uses a ``TenantSession``
bound to exactly one organization (``TenantSessionFactory.open_tenant_session``).
The binding comes only from the server-side identity (``app.auth.identity``),
never from a request parameter, body, plan or tool argument.

Reads: every ORM SELECT/UPDATE/DELETE that involves a tenant-owned model
(``TenantMixin`` or ``OrganizationMembership``) is given
``organization_id == <bound organization>`` through ``with_loader_criteria``,
which also reaches relationship lazy loads, joined/eager loads, joins and
aliases, and ``Session.get``. A row of another organization behaves as if it
did not exist.

Writes: at flush, a new tenant row without an organization gets the bound one;
a new row naming another organization, an existing row whose organization is
changed, and a row that references an already-loaded row of another
organization are all refused (``TenantIsolationError``). Nothing is ever
"fixed" silently.

Fail closed: a session with no organization bound refuses tenant models (an
exception where the statement names them, and an always-false criterion as a
backstop for any it does not).

``AuthAccount`` and ``Organization`` are global and never filtered. The only
statements an unbound session may run against memberships are identity
lookups marked with ``IDENTITY_LOOKUP`` (sign-in, ``app.auth.identity``); such a
statement may involve nothing but the identity models.

These hooks cover the ORM only. Raw ``text()`` SQL and Core ``Table``
statements are not filtered; ``tests/test_phase22b_tenant_isolation.py`` keeps
them out of runtime code. The privileged, unfiltered path is
``app.db.system_session`` -- for schema, migration, seed and maintenance
scripts only.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from sqlalchemy import Engine, event, false, inspect
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker, with_loader_criteria
from sqlalchemy.orm.base import NO_VALUE

from app.db.base import TenantMixin
from app.db.models.auth import AuthAccount
from app.db.models.organization import Organization, OrganizationMembership

ORGANIZATION_KEY = "campusnexus.organization_id"
TENANT_KEY = "campusnexus.tenant_session"
# Execution option for the identity lookups of app.auth.identity (sign-in before any organization is known).
IDENTITY_LOOKUP = "campusnexus_identity_lookup"
IDENTITY_MODELS = frozenset({AuthAccount, Organization, OrganizationMembership})


class TenantIsolationError(RuntimeError):
    """A database operation would cross, or ignore, the organization boundary."""


class TenantSession(Session):
    """A Session subclass: the isolation hooks below apply to it and to nothing else."""


def is_tenant_model(cls: type) -> bool:
    return isinstance(cls, type) and (issubclass(cls, TenantMixin) or cls is OrganizationMembership)


def session_organization(session: Session) -> Optional[int]:
    return session.info.get(ORGANIZATION_KEY)


def bind_organization(session: Session, organization_id: int) -> None:
    """Bind a tenant session to its organization. Once bound, it can never be re-bound elsewhere."""
    if not session.info.get(TENANT_KEY):
        raise TenantIsolationError("only a TenantSession can be bound to an organization")
    if not isinstance(organization_id, int) or isinstance(organization_id, bool):
        raise TenantIsolationError("an organization id must be an integer")
    current = session.info.get(ORGANIZATION_KEY)
    if current is not None and current != organization_id:
        raise TenantIsolationError("this session is already bound to another organization")
    session.info[ORGANIZATION_KEY] = organization_id


class TenantSessionFactory:
    """Creates tenant sessions for one engine. Callable like a sessionmaker: ``factory()`` is an *unbound*
    session (a request's session before its identity is resolved), which refuses tenant models."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._maker = sessionmaker(bind=engine, class_=TenantSession, expire_on_commit=False)

    @classmethod
    def from_sessionmaker(cls, factory: Any) -> "TenantSessionFactory":
        if isinstance(factory, TenantSessionFactory):
            return factory
        return cls(factory.kw["bind"])

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def kw(self) -> Dict[str, Any]:  # sessionmaker compatibility (``kw["bind"]``)
        return {"bind": self._engine}

    def __call__(self) -> TenantSession:
        session = self._maker()
        session.info[TENANT_KEY] = True
        return session

    def open_tenant_session(self, organization_id: int) -> TenantSession:
        session = self()
        bind_organization(session, organization_id)
        return session

    def bound(self, organization_id: int) -> Callable[[], TenantSession]:
        """A zero-argument factory of sessions bound to ``organization_id`` (for worker threads)."""
        bind_organization(self(), organization_id)  # validate eagerly
        return lambda: self.open_tenant_session(organization_id)


def open_tenant_session(factory: TenantSessionFactory, organization_id: int) -> TenantSession:
    return factory.open_tenant_session(organization_id)


# ---------------------------------------------------------------------------------------------------------
# Read isolation
# ---------------------------------------------------------------------------------------------------------


@event.listens_for(TenantSession, "do_orm_execute")
def _scope_statement(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    mapped = {m.class_ for m in state.all_mappers}
    if state.execution_options.get(IDENTITY_LOOKUP):
        if not mapped or not mapped <= IDENTITY_MODELS:
            raise TenantIsolationError("an identity lookup may only involve accounts, organizations and memberships")
        return
    organization_id = session_organization(state.session)
    if organization_id is None:
        if any(is_tenant_model(cls) for cls in mapped):
            raise TenantIsolationError("no organization is bound to this session; tenant data is not accessible")
        # Backstop for tenant entities the statement reaches without naming them (e.g. only in a join).
        state.statement = state.statement.options(
            with_loader_criteria(TenantMixin, lambda cls: false(), include_aliases=True),
            with_loader_criteria(OrganizationMembership, false(), include_aliases=True),
        )
        return
    state.statement = state.statement.options(
        with_loader_criteria(TenantMixin, lambda cls: cls.organization_id == organization_id, include_aliases=True),
        with_loader_criteria(OrganizationMembership, OrganizationMembership.organization_id == organization_id,
                             include_aliases=True),
    )


# ---------------------------------------------------------------------------------------------------------
# Write isolation
# ---------------------------------------------------------------------------------------------------------


def _check_loaded_references(obj: Any, organization_id: int) -> None:
    """A tenant row must not point at an already-loaded row of another organization (no extra queries)."""
    state = inspect(obj)
    for relationship in state.mapper.relationships:
        if relationship.direction.name != "MANYTOONE":
            continue
        value = state.attrs[relationship.key].loaded_value
        if value is NO_VALUE or value is None or not is_tenant_model(type(value)):
            continue
        other = getattr(value, "organization_id", None)
        if other is not None and other != organization_id:
            raise TenantIsolationError(
                f"{type(obj).__name__}.{relationship.key} references a row of another organization"
            )


@event.listens_for(TenantSession, "before_flush")
def _stamp_and_check(session: Session, flush_context: Any, instances: Any) -> None:
    organization_id = session_organization(session)
    for obj in session.new:
        if not is_tenant_model(type(obj)):
            continue
        if organization_id is None:
            raise TenantIsolationError(f"cannot write {type(obj).__name__}: no organization is bound to this session")
        if obj.organization_id is None:
            obj.organization_id = organization_id
        elif obj.organization_id != organization_id:
            raise TenantIsolationError(f"a new {type(obj).__name__} names another organization")
        _check_loaded_references(obj, organization_id)
    for obj in session.dirty:
        if not is_tenant_model(type(obj)):
            continue
        history = inspect(obj).attrs.organization_id.history
        if history.has_changes():
            raise TenantIsolationError(f"{type(obj).__name__} rows cannot move between organizations")
        if organization_id is None or obj.organization_id != organization_id:
            raise TenantIsolationError(f"cannot update {type(obj).__name__} outside the bound organization")
        _check_loaded_references(obj, organization_id)
    for obj in session.deleted:
        if is_tenant_model(type(obj)) and (organization_id is None or obj.organization_id != organization_id):
            raise TenantIsolationError(f"cannot delete {type(obj).__name__} outside the bound organization")
