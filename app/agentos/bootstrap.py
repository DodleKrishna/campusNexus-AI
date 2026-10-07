"""Builds the AgentOS kernel with every registered agent (shared by the API and the worker script)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from app.agentos.assignment_guardian import register_assignment_guardian
from app.agentos.nexus import register_nexus
from app.agentos.registry import AgentRegistry, ToolRegistry
from app.agentos.runtime import AgentRuntime
from app.db.base import utc_now
from app.rules.assignment_rules import FollowupPolicy


def build_agent_runtime(brain: Any, *, clock: Callable[[], datetime] = utc_now,
                        guardian_policy: Optional[FollowupPolicy] = None) -> AgentRuntime:
    """Phase 2: Nexus (read-only). Phase 3: the Assignment Guardian (autonomous; created by publishing)."""
    agents, tools = AgentRegistry(), ToolRegistry()
    register_nexus(agents, tools)
    register_assignment_guardian(agents, tools, guardian_policy)
    return AgentRuntime(agents, tools, brain, clock=clock)
