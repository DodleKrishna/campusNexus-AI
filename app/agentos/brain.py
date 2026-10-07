"""The AgentBrain interface: given a safe AgentContext, return one typed AgentDecision.

Provider-backed brains (``app.agentos.providers``, Phase 2) return the same
``AgentDecision`` through structured output. The runtime re-validates
whatever a brain returns, so a brain can never bypass the allowlists.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, List, Protocol, Union, runtime_checkable

from app.agentos.schemas import AgentContext, AgentDecision


class BrainUnavailableError(RuntimeError):
    """The brain could not answer right now (infrastructure). The mission is left unchanged.

    ``code`` (e.g. PROVIDER_TIMEOUT, PROVIDER_RATE_LIMITED, AI_BUDGET_EXCEEDED) is what the caller
    sees; the message is never persisted.
    """

    def __init__(self, message: str = "brain unavailable", *, code: str = "BRAIN_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code


class BrainOutputError(RuntimeError):
    """The brain answered with something that is not a usable decision (malformed JSON, extra fields,
    an offered-kind violation). The runtime rejects it like any invalid decision: nothing is executed."""

    def __init__(self, code: str = "MALFORMED_OUTPUT") -> None:
        super().__init__(code)
        self.code = code


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
