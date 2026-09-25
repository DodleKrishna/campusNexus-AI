"""Phase 22B: the privileged database path -- unfiltered, organization-agnostic sessions.

For schema initialization, migrations, controlled seeds, integrity checks and
maintenance scripts ONLY. Request handlers, services, agents, tools, the
orchestrator and RAG must never import this module (or build a plain
``sessionmaker``): they use ``app.db.tenant_session``.
``tests/test_phase22b_tenant_isolation.py`` enforces that.

There is deliberately no switch that turns tenant filtering off inside a tenant
session; code that needs to see every organization runs here, as a script.
"""
from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker


def system_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def open_system_session(engine: Engine) -> Session:
    return system_session_factory(engine)()
