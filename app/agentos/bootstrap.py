"""Builds the AgentOS kernel with every registered agent (shared by the API and the worker script)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from app.agentos.assignment_guardian import register_assignment_guardian
from app.agentos.attendance_guardian import policy_from_env as attendance_policy_from_env, register_attendance_guardian
from app.agentos.exam_guardian import policy_from_env as exam_policy_from_env, register_exam_guardian
from app.agentos.nexus import register_nexus
from app.agentos.registry import AgentRegistry, ToolRegistry
from app.agentos.runtime import AgentRuntime
from app.communication.agent import register_communication_agent
from app.communication.connectors import ConnectorRegistry
from app.communication.service import policy_from_env as communication_policy_from_env
from app.communication.sources import SourceAdapter, build_adapters
from app.communication.voice.exotel import ExotelConfig, ExotelVoiceConnector
from app.db.base import utc_now
from app.rules.assignment_rules import FollowupPolicy
from app.rules.attendance_intervention import AttendancePolicy
from app.rules.communication_policy import CommunicationPolicy
from app.rules.exam_rules import ExamPolicy


@dataclass(frozen=True)
class CommunicationSetup:
    """What the Communication Agent was registered with; the communication worker uses the same (``runtime.communication``)."""

    connectors: ConnectorRegistry
    adapters: Dict[str, SourceAdapter]
    policy: CommunicationPolicy


def build_agent_runtime(brain: Any, *, clock: Callable[[], datetime] = utc_now,
                        guardian_policy: Optional[FollowupPolicy] = None, exam_policy: Optional[ExamPolicy] = None,
                        attendance_policy: Optional[AttendancePolicy] = None,
                        communication_policy: Optional[CommunicationPolicy] = None,
                        connectors: Optional[ConnectorRegistry] = None) -> AgentRuntime:
    """Phase 2: Nexus (read-only). Phase 3: the Assignment Guardian. Phase 4: the Exam and Attendance Guardians
    (autonomous; created only by exam scheduling / deterministic absence detection). Phase 5: the Communication Agent
    (autonomous; created only by the intake of a Guardian's follow-up request). Policies default to the env."""
    exam_policy = exam_policy or exam_policy_from_env()
    attendance_policy = attendance_policy or attendance_policy_from_env()
    communication_policy = communication_policy or communication_policy_from_env()
    # In-app always; Exotel voice only when fully configured (EXOTEL_* + CAMPUSNEXUS_PUBLIC_BASE_URL), else unavailable.
    connectors = connectors or ConnectorRegistry([ExotelVoiceConnector(ExotelConfig.from_env(), policy=communication_policy)])
    setup = CommunicationSetup(connectors, build_adapters(exam_policy, attendance_policy), communication_policy)
    agents, tools = AgentRegistry(), ToolRegistry()
    register_nexus(agents, tools)
    register_assignment_guardian(agents, tools, guardian_policy)
    register_exam_guardian(agents, tools, exam_policy)
    register_attendance_guardian(agents, tools, attendance_policy)
    register_communication_agent(agents, tools, setup.connectors, setup.adapters, setup.policy)
    runtime = AgentRuntime(agents, tools, brain, clock=clock)
    runtime.communication = setup  # type: ignore[attr-defined]
    return runtime
