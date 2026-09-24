"""LangGraph state schema for the Mission Orchestrator.

A plain ``TypedDict`` with default ("last write wins") reducer semantics --
every node function that updates a dict/list-shaped key returns the *full*
new value for that key (built from the current state), rather than relying
on LangGraph's ``Annotated`` reducer machinery. This keeps the state shape
simple and every node's behavior easy to reason about and unit-test in
isolation from the compiled graph.
"""
from __future__ import annotations

from typing import Dict, List, Optional, TypedDict

from app.schemas.agent import AgentResult
from app.schemas.enums import MissionStatus, TaskStatus, UserRole
from app.schemas.mission import MissionPlan
from app.schemas.verification import VerificationResult


class OrchestratorState(TypedDict, total=False):
    """The full LangGraph state for one mission run."""

    mission_id: str
    user_id: str
    user_role: UserRole
    original_goal: str
    student_id: str
    as_of: Optional[str]

    plan: Optional[MissionPlan]
    validation_errors: List[str]
    # Set when the planner itself raised (e.g. the live LLM was unreachable or
    # returned an unusable plan) -- routes straight to finalize as FAILED.
    planning_error: Optional[str]

    task_status: Dict[str, TaskStatus]
    ready_task_ids: List[str]
    agent_results: Dict[str, AgentResult]
    verifications: Dict[str, VerificationResult]
    responses: Dict[str, str]

    mission_status: MissionStatus
    replan_count: int
    max_replans: int

    errors: List[str]
    final_result: Optional[str]
