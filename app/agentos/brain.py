"""The AgentBrain interface: given a safe AgentContext, return one typed AgentDecision.

Provider-backed brains (Groq/Anthropic/...) arrive in Phase 2 and must return the
same ``AgentDecision`` through structured output. The runtime re-validates
whatever a brain returns, so a brain can never bypass the allowlists.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, List, Protocol, Union, runtime_checkable

from app.agentos.schemas import AgentContext, AgentDecision


class BrainUnavailableError(RuntimeError):
    """The brain could not answer right now (infrastructure). The mission is left unchanged."""


@runtime_checkable
class AgentBrain(Protocol):
    def decide(self, context: AgentContext) -> Union[AgentDecision, dict]:
        """Return the next decision. Never execute anything."""


class UnconfiguredBrain:
    """Default until Phase 2 wires a provider: refuses explicitly, never guesses."""

    def decide(self, context: AgentContext) -> AgentDecision:
        raise BrainUnavailableError("no AgentBrain is configured")


class ScriptedAgentBrain:
    """Deterministic brain for tests and demos: returns pre-scripted decisions (or callables of the context).

    Items may be ``AgentDecision``s, raw dicts (to exercise validation), or ``callable(context)``.
    Records every context it was shown, so tests can assert what the brain could see.
    """

    def __init__(self, script: Iterable[Union[AgentDecision, dict, Callable[[AgentContext], Any]]]) -> None:
        self._script: List[Any] = list(script)
        self.seen: List[AgentContext] = []

    def decide(self, context: AgentContext) -> Any:
        self.seen.append(context)
        if not self._script:
            raise BrainUnavailableError("script exhausted")
        item = self._script.pop(0)
        return item(context) if callable(item) else item
