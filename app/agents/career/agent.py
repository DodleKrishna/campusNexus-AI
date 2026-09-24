"""The Career Agent (CLAUDE.md component 3) -- Phase 6 vertical slice.

Conceptual flow (mirrors app/agents/academic/agent.py exactly):

    AgentMessage -> classify intent (LLM) -> fetch DB facts (read-only service)
    -> policy evidence (RAG) -> deterministic eligibility rule
    -> AgentResult -> CareerVerifier -> final explanation (LLM, verified facts only)

Never writes to the database, never calls a tool, never computes eligibility itself.
``skill_gaps`` in the returned facts is what the Orchestrator's dispatcher (Phase 6
addition) forwards to a dependent task -- e.g. the Events Agent -- as upstream context.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.rules.opportunity_eligibility import compute_eligibility
from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.career import (
    ApplicationSummary,
    CareerIntent,
    CareerResponseContext,
    OpportunityEligibility,
    OpportunityEligibilityStatus,
)
from app.schemas.enums import AgentName, AgentResultStatus, VerificationStatus
from app.schemas.evidence import Evidence
from app.schemas.verification import VerificationResult
from app.services import career as career_service
from app.services.knowledge import KnowledgeService
from app.verification.career import CareerVerificationInput, CareerVerifier

_INTERNSHIP_POLICY_QUERY = "internship eligibility CGPA department year skill requirements"


@dataclass
class CareerAgentOutcome:
    """Local convenience bundle -- mirrors AcademicAgentOutcome, not a cross-boundary schema."""

    agent_result: AgentResult
    verification: VerificationResult
    response_text: str


def _within_reach(eligibility: OpportunityEligibility) -> bool:
    """True if the only thing standing between the student and eligibility is a skill.

    Used to keep ``skill_gaps`` actionable for a preparation plan: skills
    missing from an opportunity the student's CGPA/department/year/deadline
    already rule out (e.g. a different department's internship) aren't
    something learning a skill would fix, so they're excluded.
    """
    if eligibility.status == OpportunityEligibilityStatus.ELIGIBLE:
        return True  # already eligible -- any missing *recommended* skill is still a genuine, reachable gap
    non_skill_reasons = [r for r in eligibility.blocking_reasons if not r.startswith("missing mandatory skill")]
    return not non_skill_reasons and bool(eligibility.missing_mandatory_skills)


def _resolve_now(as_of_raw: object) -> datetime:
    if as_of_raw:
        d = date.fromisoformat(str(as_of_raw))
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


class CareerAgent:
    """Handles one career AgentMessage end-to-end. Read-only; no tool calls."""

    def __init__(
        self,
        *,
        session: Session,
        knowledge_service: KnowledgeService,
        llm_provider: LLMProvider,
        verifier: Optional[CareerVerifier] = None,
    ) -> None:
        self._session = session
        self._knowledge = knowledge_service
        self._llm = llm_provider
        self._verifier = verifier or CareerVerifier()

    def handle(self, message: AgentMessage) -> CareerAgentOutcome:
        student_id = str(message.facts.get("student_id") or "").strip()
        query = str(message.facts.get("query") or "").strip()
        now = _resolve_now(message.facts.get("as_of"))

        student = career_service.get_career_profile(self._session, student_id) if student_id else None
        intent_result = self._llm.classify_career_intent(query)
        intent = intent_result.intent

        requires_opportunities = intent == CareerIntent.OPPORTUNITY_DISCOVERY
        requires_policy_evidence = intent in (CareerIntent.OPPORTUNITY_DISCOVERY, CareerIntent.POLICY_QUESTION)

        eligibilities: List[OpportunityEligibility] = []
        applications: List[ApplicationSummary] = []
        evidence: List[Evidence] = []
        errors: List[str] = []

        if student is not None:
            if requires_opportunities:
                opportunities = career_service.get_open_opportunities(self._session)
                applications = career_service.get_applications(self._session, student_id)
                applications_by_title = {a.opportunity_title: a for a in applications}
                evidence = self._knowledge.search(_INTERNSHIP_POLICY_QUERY, as_of=now.date(), top_k=3, visibility="public")
                for opportunity in opportunities:
                    existing = applications_by_title.get(opportunity.title)
                    eligibilities.append(
                        compute_eligibility(
                            student,
                            opportunity,
                            now=now,
                            already_applied=existing is not None,
                            application_status=existing.status if existing else None,
                        )
                    )
            elif intent == CareerIntent.APPLICATION_STATUS:
                applications = career_service.get_applications(self._session, student_id)
            elif intent == CareerIntent.POLICY_QUESTION:
                evidence = self._knowledge.search(query, as_of=now.date(), top_k=5, visibility="public")

        skill_gaps = sorted(
            {
                skill
                for eligibility in eligibilities
                if _within_reach(eligibility)
                for skill in (*eligibility.missing_mandatory_skills, *eligibility.missing_recommended_skills)
            }
        )

        verification = self._verifier.verify(
            CareerVerificationInput(
                mission_id=str(message.mission_id),
                task_id=str(message.task_id),
                intent=intent,
                student=student,
                requires_opportunities=requires_opportunities,
                requires_policy_evidence=requires_policy_evidence,
                eligibilities=eligibilities,
                applications=applications,
                evidence=evidence,
            )
        )

        facts: dict = {"intent": intent.value}
        if eligibilities:
            facts["eligibilities"] = [e.model_dump(mode="json") for e in eligibilities]
        if eligibilities:
            # Published even when empty once opportunities were evaluated, so a
            # dependent Events task can tell "no gaps found" apart from "no
            # Career input" and never falls back to recommending everything.
            facts["skill_gaps"] = skill_gaps
        if applications:
            facts["applications"] = [a.model_dump(mode="json") for a in applications]

        status_map = {
            VerificationStatus.VERIFIED: AgentResultStatus.SUCCESS,
            VerificationStatus.NEEDS_REVIEW: AgentResultStatus.PARTIAL,
            VerificationStatus.FAILED: AgentResultStatus.FAILED,
        }
        agent_result = AgentResult(
            mission_id=message.mission_id,
            task_id=message.task_id,
            agent=AgentName.CAREER_AGENT,
            status=status_map[verification.status],
            facts=facts,
            evidence=evidence,
            errors=errors + verification.issues,
        )

        response_text = self._llm.generate_career_response(
            CareerResponseContext(
                intent=intent,
                verification_status=verification.status,
                verification_issues=verification.issues,
                student_name=student.full_name if student is not None else None,
                eligibilities=eligibilities,
                skill_gaps=skill_gaps,
                applications=applications,
                evidence=evidence,
                errors=errors,
            )
        )

        return CareerAgentOutcome(agent_result=agent_result, verification=verification, response_text=response_text)
