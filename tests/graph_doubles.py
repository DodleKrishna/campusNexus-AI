"""Deterministic test-only doubles for orchestrator tests.

CLAUDE.md/Phase 5 spec: "test parallel execution using deterministic test
agents registered only inside tests. Do not create additional production
specialist agents." Nothing here is imported by application code under
``app/`` -- it exists solely so ``tests/test_orchestrator*.py`` can exercise
DAG scheduling, failure/retry, and NEEDS_REVIEW pausing without depending on
the real (read-only, always-succeeds-or-fails-for-real-reasons) Academic
Agent or a live/mocked LLM for planning.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.schemas.academic import AcademicIntentResult, AcademicResponseContext, CourseSummary
from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.enums import AgentName, AgentResultStatus, VerificationPhase, VerificationStatus
from app.schemas.mission import MissionPlan
from app.schemas.verification import VerificationResult


@dataclass
class FakeOutcome:
    """Satisfies app.graph.results.AgentOutcome structurally."""

    agent_result: AgentResult
    verification: VerificationResult
    response_text: str


def _outcome(message: AgentMessage, *, agent_status: AgentResultStatus, verification_status: VerificationStatus) -> FakeOutcome:
    return FakeOutcome(
        agent_result=AgentResult(
            mission_id=message.mission_id,
            task_id=message.task_id,
            agent=message.target,
            status=agent_status,
            facts={"objective": message.objective},
        ),
        verification=VerificationResult(
            verification_id=f"v-{message.task_id}",
            mission_id=message.mission_id,
            task_id=message.task_id,
            phase=VerificationPhase.POST_ACTION,
            status=verification_status,
        ),
        response_text=f"[{verification_status.value}] {message.objective}",
    )


class SuccessAgent:
    """Always succeeds and verifies -- for parallel/dependency-chain happy-path tests."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def handle(self, message: AgentMessage) -> FakeOutcome:
        return _outcome(message, agent_status=AgentResultStatus.SUCCESS, verification_status=VerificationStatus.VERIFIED)


class AlwaysFailAgent:
    """Always fails, deterministically -- for retry-limit / cascade tests."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def handle(self, message: AgentMessage) -> FakeOutcome:
        return _outcome(message, agent_status=AgentResultStatus.FAILED, verification_status=VerificationStatus.FAILED)


class ChangingFailureAgent:
    """Always fails, but with a *different* reason every attempt -- each retry
    brings new information, so Phase 10 duplicate-failure suppression must not
    stop it; only ``max_replans`` bounds it."""

    _attempts = 0
    _lock = threading.Lock()

    def __init__(self, session: Session) -> None:
        self._session = session

    def handle(self, message: AgentMessage) -> FakeOutcome:
        with ChangingFailureAgent._lock:
            ChangingFailureAgent._attempts += 1
            attempt = ChangingFailureAgent._attempts
        outcome = _outcome(message, agent_status=AgentResultStatus.FAILED, verification_status=VerificationStatus.FAILED)
        outcome.verification.issues.append(f"upstream data changed (observation #{attempt})")
        return outcome


class NeedsReviewAgent:
    """Always comes back NEEDS_REVIEW -- for pause-not-auto-approve tests."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def handle(self, message: AgentMessage) -> FakeOutcome:
        return _outcome(message, agent_status=AgentResultStatus.PARTIAL, verification_status=VerificationStatus.NEEDS_REVIEW)


class MismatchedVerificationAgent:
    """Agent invocation succeeds (no exception, AgentResultStatus.SUCCESS) but
    the *verification* comes back FAILED -- proves the Orchestrator routes on
    ``VerificationResult.status``, never on "the call didn't raise" (CLAUDE.md:
    do not treat a successful agent invocation as proof that its result is verified)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def handle(self, message: AgentMessage) -> FakeOutcome:
        return FakeOutcome(
            agent_result=AgentResult(
                mission_id=message.mission_id,
                task_id=message.task_id,
                agent=message.target,
                status=AgentResultStatus.SUCCESS,
                facts={"objective": message.objective},
            ),
            verification=VerificationResult(
                verification_id=f"v-{message.task_id}",
                mission_id=message.mission_id,
                task_id=message.task_id,
                phase=VerificationPhase.POST_ACTION,
                status=VerificationStatus.FAILED,
                issues=["post-condition check failed despite a successful-looking agent call"],
            ),
            response_text="verification failed",
        )


class RaisingAgent:
    """Raises an unhandled exception -- for the dispatcher's crash-containment test."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def handle(self, message: AgentMessage) -> FakeOutcome:
        raise RuntimeError("simulated agent crash")


@dataclass
class FixedPlanLLMProvider(LLMProvider):
    """A test-only LLMProvider whose plan_mission returns a pre-built plan, ignoring the goal text.

    Lets tests hand-construct a MissionPlan with real dependencies/agent
    assignments (to exercise the scheduler/validator through the real
    Orchestrator) without needing a goal string the mock planner's
    clause-splitting heuristic would have to parse correctly.
    """

    name: str = "fixed-plan-test-double"
    plan_factory: Optional[Callable[[str, str], MissionPlan]] = None

    def classify_academic_intent(self, query: str, enrolled_courses: List[CourseSummary]) -> AcademicIntentResult:
        raise NotImplementedError("FixedPlanLLMProvider is planning-only; not expected to classify intent")

    def generate_academic_response(self, context: AcademicResponseContext) -> str:
        raise NotImplementedError("FixedPlanLLMProvider is planning-only; not expected to render a response")

    def plan_mission(self, mission_id: str, goal: str, *, supported_agents: List[AgentName]) -> MissionPlan:
        assert self.plan_factory is not None, "FixedPlanLLMProvider requires plan_factory"
        return self.plan_factory(mission_id, goal)

    def classify_career_intent(self, query: str):
        raise NotImplementedError("FixedPlanLLMProvider is planning-only; not expected to classify intent")

    def generate_career_response(self, context) -> str:
        raise NotImplementedError("FixedPlanLLMProvider is planning-only; not expected to render a response")

    def classify_events_intent(self, query: str):
        raise NotImplementedError("FixedPlanLLMProvider is planning-only; not expected to classify intent")

    def generate_events_response(self, context) -> str:
        raise NotImplementedError("FixedPlanLLMProvider is planning-only; not expected to render a response")

    def classify_services_intent(self, query: str):
        raise NotImplementedError("FixedPlanLLMProvider is planning-only; not expected to classify intent")

    def generate_services_response(self, context) -> str:
        raise NotImplementedError("FixedPlanLLMProvider is planning-only; not expected to render a response")
