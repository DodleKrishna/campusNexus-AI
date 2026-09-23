"""The Events & Opportunity Agent (CLAUDE.md component 4) -- Phase 6 vertical slice.

Conceptual flow (mirrors app/agents/academic/agent.py):

    AgentMessage -> classify intent (LLM) -> read upstream context (if this
    task depends on other tasks -- Academic timetable/exams, Career
    skill_gaps, merged in by app/graph/dispatcher.py) -> deterministic
    relevance/availability/conflict rules -> AgentResult -> EventsVerifier
    -> final explanation (LLM, verified facts only)

Relevance matching is deterministic keyword overlap between (this task's own
query text) union (any upstream ``skill_gaps``) and each event's title/
category/organizer -- never an LLM judgment call, so an event is never
claimed "relevant" without a traceable shared keyword. Never writes to the
database, never calls a tool.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.db.repositories.students import get_student_by_id
from app.llm.base import LLMProvider
from app.rules.event_availability import assess_event
from app.schemas.academic import ExamEntry, TimetableEntry
from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.enums import AgentName, AgentResultStatus, VerificationStatus
from app.schemas.events import EventAssessment, EventsIntent, EventsResponseContext
from app.schemas.evidence import Evidence
from app.schemas.verification import VerificationResult
from app.services import events as events_service
from app.services.knowledge import KnowledgeService
from app.verification.events import EventsVerificationInput, EventsVerifier

_EVENT_POLICY_QUERY = "event registration procedure capacity deadline conflicts"

_RELEVANCE_STOPWORDS = {
    "find", "workshops", "workshop", "related", "and", "identify", "skill", "skills", "gaps", "gap",
    "that", "don", "conflict", "conflicts", "with", "your", "classes", "class", "exams", "exam",
    "events", "event", "for", "the", "are", "when", "what", "register", "registration", "status", "my",
}
_WORD_RE = re.compile(r"[a-zA-Z0-9]{3,}")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}\b")


def _tokenize_for_relevance(text: str) -> Set[str]:
    words = {w.lower() for w in _WORD_RE.findall(text) if w.lower() not in _RELEVANCE_STOPWORDS}
    acronyms = {a.lower() for a in _ACRONYM_RE.findall(text)}
    return words | acronyms


def _resolve_now(as_of_raw: object) -> datetime:
    if as_of_raw:
        d = date.fromisoformat(str(as_of_raw))
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _parse_upstream_timetable(facts: Dict) -> Optional[List[TimetableEntry]]:
    raw = facts.get("timetable")
    if raw is None:
        return None
    return [TimetableEntry.model_validate(item) for item in raw]


def _parse_upstream_exams(facts: Dict) -> Optional[List[ExamEntry]]:
    raw = facts.get("exams")
    if raw is None:
        return None
    return [ExamEntry.model_validate(item) for item in raw]


@dataclass
class EventsAgentOutcome:
    """Local convenience bundle -- mirrors AcademicAgentOutcome, not a cross-boundary schema."""

    agent_result: AgentResult
    verification: VerificationResult
    response_text: str


class EventsAgent:
    """Handles one events/opportunity AgentMessage end-to-end. Read-only; no tool calls; no registration."""

    def __init__(
        self,
        *,
        session: Session,
        knowledge_service: KnowledgeService,
        llm_provider: LLMProvider,
        verifier: Optional[EventsVerifier] = None,
    ) -> None:
        self._session = session
        self._knowledge = knowledge_service
        self._llm = llm_provider
        self._verifier = verifier or EventsVerifier()

    def handle(self, message: AgentMessage) -> EventsAgentOutcome:
        student_id = str(message.facts.get("student_id") or "").strip()
        query = str(message.facts.get("query") or "").strip()
        now = _resolve_now(message.facts.get("as_of"))

        student = get_student_by_id(self._session, student_id) if student_id else None
        student_exists = student is not None

        intent_result = self._llm.classify_events_intent(query)
        intent = intent_result.intent

        requires_events = intent in (EventsIntent.EVENT_DISCOVERY, EventsIntent.REGISTRATION_STATUS)
        requires_policy_evidence = intent in (EventsIntent.EVENT_DISCOVERY, EventsIntent.POLICY_QUESTION)

        timetable = _parse_upstream_timetable(message.facts)
        exams = _parse_upstream_exams(message.facts)
        expected_conflict_check = timetable is not None or exams is not None

        raw_skill_gaps = message.facts.get("skill_gaps") or []
        relevance_tokens = _tokenize_for_relevance(query)
        for gap in raw_skill_gaps:
            relevance_tokens |= _tokenize_for_relevance(str(gap))

        assessments: List[EventAssessment] = []
        evidence: List[Evidence] = []
        errors: List[str] = []

        if student_exists:
            if requires_events:
                for event in events_service.get_upcoming_events(self._session, now):
                    event_tokens = _tokenize_for_relevance(f"{event.title} {event.description} {event.category}")
                    if relevance_tokens and not (relevance_tokens & event_tokens):
                        continue  # not relevant to this query/skill-gap set -- never claim otherwise
                    registration_status = events_service.get_student_registration_status(
                        self._session, student_id, event.event_id
                    )
                    confirmed_count = events_service.get_registration_count(self._session, event.event_id)
                    assessments.append(
                        assess_event(
                            event,
                            now=now,
                            confirmed_registrations=confirmed_count,
                            already_registered=registration_status is not None,
                            registration_status=registration_status,
                            timetable=timetable,
                            exams=exams,
                        )
                    )
                evidence = self._knowledge.search(_EVENT_POLICY_QUERY, as_of=now.date(), top_k=3, visibility="public")
            elif intent == EventsIntent.POLICY_QUESTION:
                evidence = self._knowledge.search(query, as_of=now.date(), top_k=5, visibility="public")

        verification = self._verifier.verify(
            EventsVerificationInput(
                mission_id=str(message.mission_id),
                task_id=str(message.task_id),
                intent=intent,
                student_exists=student_exists,
                requires_events=requires_events,
                requires_policy_evidence=requires_policy_evidence,
                expected_conflict_check=expected_conflict_check,
                assessments=assessments,
                evidence=evidence,
            )
        )

        facts: dict = {"intent": intent.value}
        if assessments:
            facts["assessments"] = [a.model_dump(mode="json") for a in assessments]

        status_map = {
            VerificationStatus.VERIFIED: AgentResultStatus.SUCCESS,
            VerificationStatus.NEEDS_REVIEW: AgentResultStatus.PARTIAL,
            VerificationStatus.FAILED: AgentResultStatus.FAILED,
        }
        agent_result = AgentResult(
            mission_id=message.mission_id,
            task_id=message.task_id,
            agent=AgentName.EVENTS_OPPORTUNITY_AGENT,
            status=status_map[verification.status],
            facts=facts,
            evidence=evidence,
            errors=errors + verification.issues,
        )

        response_text = self._llm.generate_events_response(
            EventsResponseContext(
                intent=intent,
                verification_status=verification.status,
                verification_issues=verification.issues,
                student_name=student.user.full_name if student is not None else None,
                assessments=assessments,
                evidence=evidence,
                errors=errors,
            )
        )

        return EventsAgentOutcome(agent_result=agent_result, verification=verification, response_text=response_text)
