"""LangGraph checkpointing strategy.

An ``InMemorySaver`` gives the compiled graph real LangGraph checkpoint
semantics -- ``get_state``/``update_state``, resuming a paused ``invoke``
within the same process (``thread_id`` = ``mission_id``) -- useful for
stepping through a single mission run, e.g. inspecting state right after a
NEEDS_REVIEW pause. It is intentionally *not* the durable source of truth:
it lives in process memory and is lost on process restart.

The Context Service (app/services/context.py, SQLAlchemy/SQLite) remains
the single source of truth across process restarts, per CLAUDE.md component
10. Cross-process mission resumption
(``app.graph.orchestrator.MissionOrchestrator.resume_mission``) reconstructs
state from the Context Service's persisted Mission/MissionStep/AuditLog
rows, not from this checkpointer -- so a real interruption (process killed
and restarted) is still recoverable even though this checkpointer's memory
is gone. See docs/ARCHITECTURE.md's "Mission Orchestrator" section.
"""
from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver


def build_checkpointer() -> InMemorySaver:
    """A fresh, process-local LangGraph checkpointer for one Orchestrator instance."""
    return InMemorySaver()
