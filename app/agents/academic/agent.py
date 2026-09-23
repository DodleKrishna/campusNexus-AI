"""The Academic Agent (CLAUDE.md component 2) -- Phase 4 vertical slice.

Conceptual flow (CLAUDE.md core principle applied end-to-end):

    AgentMessage -> classify intent (LLM) -> resolve course (deterministic)
    -> fetch DB facts (read-only service) + policy evidence (RAG)
    -> deterministic rule(s) -> AgentResult -> AcademicVerifier
    -> final explanation (LLM, verified facts only)

Never writes to the database, never calls a tool, never computes an
official-rule result itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from sqlalchemy.orm import Session

from app.agents.academic.course_resolution import resolve_course
from app.llm.base import LLMProvider
from app.rules.attendance import compute_attendance
from app.rules.eligibility import compute_exam_eligibility
from app.rules.policy_threshold import extract_attendance_threshold
from app.schemas.academic import (
    AcademicIntent,
    AcademicResponseContext,
    AttendanceCalculation,
    CourseResolution,
    CourseResolutionStatus,
    CourseSummary,
    ExamEligibilityResult,
    ExamEntry,
    PolicyThreshold,
    ThresholdExtractionStatus,
    TimetableEntry,
)
from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.enums import AgentName, AgentResultStatus, VerificationStatus
from app.schemas.evidence import Evidence
from app.schemas.verification import VerificationResult
from app.services import academic as academic_service
from app.services.knowledge import KnowledgeService
from app.verification.academic import AcademicVerificationInput, AcademicVerifier

_ATTENDANCE_INTENTS = frozenset(
    {AcademicIntent.ATTENDANCE_STATUS, AcademicIntent.ATTENDANCE_RECOVERY, AcademicIntent.EXAM_ELIGIBILITY}
)


@dataclass
class AcademicAgentOutcome:
    """Local convenience bundle for callers (demo runner, eval, tests).

    Not a cross-boundary schema -- mirrors the ``StudentContext`` dataclass
    convention already used in app/services/context.py for the same reason:
    it's an aggregate of already-defined Pydantic outputs for local use, not
    itself an agent-to-agent message.
    """

    agent_result: AgentResult
    verification: VerificationResult
    response_text: str


class AcademicAgent:
    """Handles one academic AgentMessage end-to-end. Read-only; no tool calls."""

    def __init__(
        self,
        *,
        session: Session,
        knowledge_service: KnowledgeService,
        llm_provider: LLMProvider,
        verifier: Optional[AcademicVerifier] = None,
    ) -> None:
        self._session = session
        self._knowledge = knowledge_service
        self._llm = llm_provider
        self._verifier = verifier or AcademicVerifier()

    def handle(self, message: AgentMessage) -> AcademicAgentOutcome:
        student_id = str(message.facts.get("student_id") or "").strip()
        query = str(message.facts.get("query") or "").strip()
        as_of_raw = message.facts.get("as_of")
        as_of = date.fromisoformat(str(as_of_raw)) if as_of_raw else date.today()

        student = (
            academic_service.get_student_academic_profile(self._session, student_id) if student_id else None
        )
        enrolled_courses: List[CourseSummary] = student.courses if student is not None else []

        intent_result = self._llm.classify_academic_intent(query, enrolled_courses)
        intent = intent_result.intent
        course_resolution = resolve_course(intent_result.raw_course_reference, enrolled_courses)

        requires_course = intent in _ATTENDANCE_INTENTS
        requires_attendance = intent in _ATTENDANCE_INTENTS
        requires_policy_evidence = intent in _ATTENDANCE_INTENTS or intent == AcademicIntent.POLICY_QUESTION

        course_summary: Optional[CourseSummary] = None
        attendance_calc: Optional[AttendanceCalculation] = None
        threshold: Optional[PolicyThreshold] = None
        eligibility: Optional[ExamEligibilityResult] = None
        evidence: List[Evidence] = []
        timetable: List[TimetableEntry] = []
        exams: List[ExamEntry] = []
        errors: List[str] = []

        if student is not None:
            if requires_course and course_resolution.status == CourseResolutionStatus.RESOLVED:
                course_summary = next(
                    (c for c in enrolled_courses if c.course_code == course_resolution.course_code), None
                )

            if requires_attendance and course_resolution.status == CourseResolutionStatus.RESOLVED:
                evidence = self._knowledge.get_active_policy("attendance_policy", as_of=as_of, visibility="public")
                threshold = extract_attendance_threshold(evidence)
                if threshold.status == ThresholdExtractionStatus.OK:
                    snapshots = academic_service.get_attendance(
                        self._session, student_id, course_resolution.course_code
                    )
                    if snapshots:
                        snapshot = snapshots[0]
                        attendance_calc = compute_attendance(
                            snapshot.classes_attended, snapshot.classes_conducted, threshold.required_percentage
                        )
                        if intent == AcademicIntent.EXAM_ELIGIBILITY:
                            eligibility = compute_exam_eligibility(attendance_calc)
                    else:
                        errors.append("No attendance record found for the resolved course.")

            elif intent == AcademicIntent.POLICY_QUESTION:
                evidence = self._knowledge.search(query, as_of=as_of, top_k=5, visibility="public")

            elif intent == AcademicIntent.TIMETABLE:
                timetable = academic_service.get_timetable(self._session, student_id)
                if course_resolution.status == CourseResolutionStatus.RESOLVED:
                    timetable = [t for t in timetable if t.course_code == course_resolution.course_code]

            elif intent == AcademicIntent.EXAM_SCHEDULE:
                exams = academic_service.get_exam_schedule(self._session, student_id)
                if course_resolution.status == CourseResolutionStatus.RESOLVED:
                    exams = [e for e in exams if e.course_code == course_resolution.course_code]

        verification = self._verifier.verify(
            AcademicVerificationInput(
                mission_id=str(message.mission_id),
                task_id=str(message.task_id),
                intent=intent,
                student=student,
                requires_course=requires_course,
                requires_attendance=requires_attendance,
                requires_policy_evidence=requires_policy_evidence,
                course_resolution=course_resolution if student is not None else None,
                attendance=attendance_calc,
                threshold=threshold,
                eligibility=eligibility,
                evidence=evidence,
            )
        )

        facts: dict = {
            "intent": intent.value,
            "course_resolution": course_resolution.model_dump(mode="json"),
        }
        if attendance_calc is not None:
            facts["attendance"] = attendance_calc.model_dump(mode="json")
        if threshold is not None:
            facts["threshold"] = threshold.model_dump(mode="json")
        if eligibility is not None:
            facts["eligibility"] = eligibility.model_dump(mode="json")
        if timetable:
            facts["timetable"] = [t.model_dump(mode="json") for t in timetable]
        if exams:
            facts["exams"] = [e.model_dump(mode="json") for e in exams]

        status_map = {
            VerificationStatus.VERIFIED: AgentResultStatus.SUCCESS,
            VerificationStatus.NEEDS_REVIEW: AgentResultStatus.PARTIAL,
            VerificationStatus.FAILED: AgentResultStatus.FAILED,
        }
        agent_result = AgentResult(
            mission_id=message.mission_id,
            task_id=message.task_id,
            agent=AgentName.ACADEMIC_AGENT,
            status=status_map[verification.status],
            facts=facts,
            evidence=evidence,
            errors=errors + verification.issues,
        )

        response_text = self._llm.generate_academic_response(
            AcademicResponseContext(
                intent=intent,
                verification_status=verification.status,
                verification_issues=verification.issues,
                student_name=student.full_name if student is not None else None,
                course=course_summary,
                course_resolution=course_resolution,
                attendance=attendance_calc,
                eligibility=eligibility,
                threshold=threshold,
                timetable=timetable,
                exams=exams,
                evidence=evidence,
                errors=errors,
            )
        )

        return AcademicAgentOutcome(agent_result=agent_result, verification=verification, response_text=response_text)
