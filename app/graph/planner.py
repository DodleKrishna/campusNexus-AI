"""Mission planning call-site.

Thin by design -- all the actual decomposition logic (deterministic in
MockLLMProvider, tool-calling in AnthropicLLMProvider) lives behind the
``LLMProvider`` abstraction (app/llm/base.py), exactly like
``app.agents.academic.agent.AcademicAgent`` calling
``classify_academic_intent``. This module is the Orchestrator's call-site,
not a second planning implementation.
"""
from __future__ import annotations

from typing import List

from app.llm.base import LLMProvider
from app.schemas.enums import AgentName
from app.schemas.mission import MissionPlan


def generate_plan(
    llm_provider: LLMProvider, mission_id: str, goal: str, *, supported_agents: List[AgentName]
) -> MissionPlan:
    """Produce a (not-yet-validated) MissionPlan for ``goal``.

    Never executed directly -- app.graph.validator.validate_plan must pass
    before app.graph.dispatcher ever dispatches a task from this plan.
    """
    return llm_provider.plan_mission(mission_id, goal, supported_agents=supported_agents)
