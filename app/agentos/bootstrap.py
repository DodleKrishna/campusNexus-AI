"""Builds the AgentOS kernel with every registered agent (shared by the API and the worker script)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from app.agentos.assignment_guardian import register_assignment_guardian
from app.agentos.attendance_guardian import register_attendance_guardian
from app.agentos.exam_guardian import register_exam_guardian
from app.agentos.nexus import register_nexus
from app.agentos.registry import AgentRegistry, ToolRegistry
from app.agentos.runtime import AgentRuntime
from app.db.base import utc_now
from app.rules.assignment_rules import FollowupPolicy
from app.rules.attendance_intervention import AttendancePolicy
from app.rules.exam_rules import ExamPolicy


def build_agent_runtime(brain: Any, *, clock: Callable[[], datetime] = utc_now,
                        guardian_policy: Optional[FollowupPolicy] = None, exam_policy: Optional[ExamPolicy] = None,
                        attendance_policy: Optional[AttendancePolicy] = None) -> AgentRuntime:
    """Phase 2: Nexus (read-only). Phase 3: the Assignment Guardian. Phase 4: the Exam and Attendance Guardians
    (autonomous; created only by exam scheduling / deterministic absence detection). Policies default to the env."""
    agents, tools = AgentRegistry(), ToolRegistry()
    register_nexus(agents, tools)
    register_assignment_guardian(agents, tools, guardian_policy)
    register_exam_guardian(agents, tools, exam_policy)
    register_attendance_guardian(agents, tools, attendance_policy)
    return AgentRuntime(agents, tools, brain, clock=clock)
