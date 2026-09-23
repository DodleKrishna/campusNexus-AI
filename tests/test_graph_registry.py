"""Unit tests for the typed agent registry (app/graph/registry.py)."""
from __future__ import annotations

import pytest

from app.graph.registry import AgentRegistry, UnsupportedAgentError
from app.schemas.enums import AgentName
from tests.graph_doubles import SuccessAgent


def test_register_and_build() -> None:
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda session: SuccessAgent(session))
    assert registry.is_supported(AgentName.ACADEMIC_AGENT)
    agent = registry.build(AgentName.ACADEMIC_AGENT, session=None)
    assert isinstance(agent, SuccessAgent)


def test_supported_agents_reflects_registrations() -> None:
    registry = AgentRegistry()
    assert registry.supported_agents() == []
    registry.register(AgentName.ACADEMIC_AGENT, lambda session: SuccessAgent(session))
    assert registry.supported_agents() == [AgentName.ACADEMIC_AGENT]


def test_unsupported_agent_raises_explicit_error() -> None:
    registry = AgentRegistry()
    with pytest.raises(UnsupportedAgentError, match="ACADEMIC_AGENT|academic_agent"):
        registry.build(AgentName.ACADEMIC_AGENT, session=None)


def test_build_uses_a_fresh_instance_each_call() -> None:
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda session: SuccessAgent(session))
    first = registry.build(AgentName.ACADEMIC_AGENT, session="session-a")
    second = registry.build(AgentName.ACADEMIC_AGENT, session="session-b")
    assert first is not second
