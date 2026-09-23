"""Typed agent registry (CLAUDE.md: agents communicate only through the
Orchestrator using their Pydantic schemas -- the registry is how the
Orchestrator finds them without hardcoding a per-agent if/elif chain).

Stores *factories*, not instances: ``Callable[[Session], SpecialistAgent]``.
A fresh ``Session`` per dispatched call (see app/graph/dispatcher.py) is what
makes concurrent dispatch of independent tasks safe -- SQLAlchemy's
``Session`` is not thread-safe, so sharing one across parallel agent calls
would be a real bug, not just a style preference.

Only the Academic Agent is registered in this phase. Career/Events/Campus
Services/Knowledge-RAG/Action agents are explicitly deferred -- this
registry is what lets them be added later (one ``register`` call each)
without touching orchestration logic.
"""
from __future__ import annotations

from typing import Callable, Dict, List

from sqlalchemy.orm import Session

from app.graph.results import SpecialistAgent
from app.schemas.enums import AgentName

AgentFactory = Callable[[Session], SpecialistAgent]


class UnsupportedAgentError(Exception):
    """Raised when a task is assigned to an agent the registry doesn't know about.

    The plan validator is the primary gate against this ever happening at
    dispatch time (CLAUDE.md: unsupported agent assignments must produce an
    explicit error, never a silent no-op) -- this is defense in depth.
    """


class AgentRegistry:
    """Maps ``AgentName`` -> a factory that builds a ready-to-use agent instance."""

    def __init__(self) -> None:
        self._factories: Dict[AgentName, AgentFactory] = {}

    def register(self, agent_name: AgentName, factory: AgentFactory) -> None:
        self._factories[agent_name] = factory

    def supported_agents(self) -> List[AgentName]:
        return list(self._factories)

    def is_supported(self, agent_name: AgentName) -> bool:
        return agent_name in self._factories

    def build(self, agent_name: AgentName, session: Session) -> SpecialistAgent:
        try:
            factory = self._factories[agent_name]
        except KeyError as exc:
            raise UnsupportedAgentError(
                f"No agent registered for {agent_name.value!r}; supported: "
                f"{[a.value for a in self.supported_agents()]}"
            ) from exc
        return factory(session)
