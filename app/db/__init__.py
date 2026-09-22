"""SQLAlchemy persistence layer for CampusNexus (Phase 2).

Submodules:
- base: declarative Base, naming convention, UTC datetime type.
- session: engine/session factory creation, database initialization.
- models: SQLAlchemy ORM models, grouped by domain.
- repositories: read-oriented query functions used by future specialist agents.

ORM models here are persistence representations. They intentionally do not
duplicate the Phase 1 Pydantic contracts in app/schemas -- those remain the
structured-output contracts exchanged across component boundaries.
"""
