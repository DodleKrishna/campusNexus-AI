"""The Campus Services Agent (CLAUDE.md component 5) -- Phase 6 vertical slice.

Conceptual flow (mirrors app/agents/academic/agent.py):

    AgentMessage -> classify intent (LLM) -> fetch case data (read-only service)
    -> deterministic SLA breach checks -> policy evidence (RAG)
    -> AgentResult -> ServicesVerifier -> final explanation (LLM, verified facts only)

Read-only: no case creation, assignment, or escalation (deferred to a later phase).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.repositories.students import get_student_by_id
from app.llm.base import LLMProvider
from app.rules.sla import compute_sla_breaches
from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.enums import AgentName, AgentResultStatus, VerificationStatus
from app.schemas.evidence import Evidence
from app.schemas.services import CaseSLAAssessment, ServicesIntent, ServicesResponseContext
from app.schemas.verification import VerificationResult
from app.services import services as services_service
from app.services.knowledge import KnowledgeService
from app.verification.services import ServicesVerificationInput, ServicesVerifier

_GRIEVANCE_POLICY_QUERY = "grievance case SLA response resolution escalation procedure"


def _resolve_now(as_of_raw: object) -> datetime:
    if as_of_raw:
        d = date.fromisoformat(str(as_of_raw))
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


@dataclass
class ServicesAgentOutcome:
    """Local convenience bundle -- mirrors AcademicAgentOutcome, not a cross-boundary schema."""

    agent_result: AgentResult
    verification: VerificationResult
    response_text: str


class ServicesAgent:
    """Handles one campus-services AgentMessage end-to-end. Read-only; no tool calls; no escalation."""

    def __init__(
        self,
        *,
        session: Session,
        knowledge_service: KnowledgeService,
        llm_provider: LLMProvider,
        verifier: Optional[ServicesVerifier] = None,
    ) -> None:
        self._session = session
        self._knowledge = knowledge_service
        self._llm = llm_provider
        self._verifier = verifier or ServicesVerifier()

    def handle(self, message: AgentMessage) -> ServicesAgentOutcome:
        student_id = str(message.facts.get("student_id") or "").strip()
        query = str(message.facts.get("query") or "").strip()
        now = _resolve_now(message.facts.get("as_of"))

        student = get_student_by_id(self._session, student_id) if student_id else None
        student_exists = student is not None

        intent_result = self._llm.classify_services_intent(query)
        intent = intent_result.intent

        requires_cases = intent == ServicesIntent.CASE_STATUS
        requires_policy_evidence = intent in (ServicesIntent.CASE_STATUS, ServicesIntent.POLICY_QUESTION)

        case_assessments: List[CaseSLAAssessment] = []
        cases_missing_sla: List[str] = []
        evidence: List[Evidence] = []
        errors: List[str] = []

        if student_exists:
            if requires_cases:
                for case in services_service.get_student_case_summaries(self._session, student_id):
                    sla = services_service.get_case_sla_record(self._session, case.case_code)
                    if sla is None:
                        cases_missing_sla.append(case.case_code)
                        continue
                    response_breached, resolution_breached = compute_sla_breaches(
                        response_due_at=sla.response_due_at,
                        resolution_due_at=sla.resolution_due_at,
                        responded_at=sla.responded_at,
                        resolved_at=sla.resolved_at,
                        now=now,
                    )
                    case_assessments.append(
                        CaseSLAAssessment(
                            case=case,
                            response_due_at=sla.response_due_at,
                            resolution_due_at=sla.resolution_due_at,
                            responded_at=sla.responded_at,
                            resolved_at=sla.resolved_at,
                            response_breached=response_breached,
                            resolution_breached=resolution_breached,
                        )
                    )
                evidence = self._knowledge.search(_GRIEVANCE_POLICY_QUERY, as_of=now.date(), top_k=3, visibility="public")
            elif intent == ServicesIntent.POLICY_QUESTION:
                evidence = self._knowledge.search(query, as_of=now.date(), top_k=5, visibility="public")

        verification = self._verifier.verify(
            ServicesVerificationInput(
                mission_id=str(message.mission_id),
                task_id=str(message.task_id),
                intent=intent,
                student_exists=student_exists,
                expected_student_code=student.student_code if student is not None else None,
                requires_cases=requires_cases,
                requires_policy_evidence=requires_policy_evidence,
                now=now,
                case_assessments=case_assessments,
                cases_missing_sla=cases_missing_sla,
                evidence=evidence,
            )
        )

        facts: dict = {"intent": intent.value}
        if case_assessments:
            facts["case_assessments"] = [a.model_dump(mode="json") for a in case_assessments]
        if cases_missing_sla:
            facts["cases_missing_sla"] = cases_missing_sla

        status_map = {
            VerificationStatus.VERIFIED: AgentResultStatus.SUCCESS,
            VerificationStatus.NEEDS_REVIEW: AgentResultStatus.PARTIAL,
            VerificationStatus.FAILED: AgentResultStatus.FAILED,
        }
        agent_result = AgentResult(
            mission_id=message.mission_id,
            task_id=message.task_id,
            agent=AgentName.CAMPUS_SERVICES_AGENT,
            status=status_map[verification.status],
            facts=facts,
            evidence=evidence,
            errors=errors + verification.issues,
        )

        response_text = self._llm.generate_services_response(
            ServicesResponseContext(
                intent=intent,
                verification_status=verification.status,
                verification_issues=verification.issues,
                student_name=student.user.full_name if student is not None else None,
                case_assessments=case_assessments,
                evidence=evidence,
                errors=errors,
            )
        )

        return ServicesAgentOutcome(agent_result=agent_result, verification=verification, response_text=response_text)
