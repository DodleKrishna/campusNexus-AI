"""Structural integration contract between the Orchestrator and any specialist agent.

Deliberately a ``typing.Protocol``, not a base class: ``app.agents.academic.agent.AcademicAgent``
and its ``AcademicAgentOutcome`` return type (both approved in Phase 4) already have exactly this
shape, so they satisfy these protocols with zero edits -- no inheritance, no adapter, no
redesign of the Academic Agent. Any future specialist agent only needs to match this shape to be
orchestrator-dispatchable.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.verification import VerificationResult


@runtime_checkable
class AgentOutcome(Protocol):
    """What a specialist agent's ``handle()`` call must return."""

    agent_result: AgentResult
    verification: VerificationResult
    response_text: str


@runtime_checkable
class SpecialistAgent(Protocol):
    """What a specialist agent instance must implement to be dispatchable."""

    def handle(self, message: AgentMessage) -> AgentOutcome: ...
