"""Deterministic, offline LLM provider.

Used by the entire test suite (CLAUDE.md: tests must not call a live LLM
API) and as the default runtime mode so the full Academic Agent workflow
works with zero external dependencies/credentials. Intent classification is
keyword rules over the query text; course-reference extraction is regex
matching against the student's own enrolled courses, falling back to a
generic "in X"/"for X" phrase capture for unknown/ambiguous references.
Response generation is plain string templates over already-verified
structured facts -- never a fresh interpretation of policy or arithmetic.
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.llm.base import LLMProvider
from app.schemas.academic import (
    AcademicIntent,
    AcademicIntentResult,
    AcademicResponseContext,
    AttendanceRuleStatus,
    CourseResolutionStatus,
    CourseSummary,
    ExamEligibilityStatus,
)
from app.schemas.enums import AgentName, VerificationStatus
from app.schemas.mission import MissionPlan, MissionTask

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_ACRONYM_STOPWORDS = {"of", "and", "the", "for", "in", "to"}
_COURSE_PHRASE_RE = re.compile(r"(?:in|for)\s+([A-Za-z][A-Za-z0-9 ]{1,40}?)(?:\?|$|,|\s+to\b)")

# Mission-planning heuristics: split a compound goal into independent
# clauses, and carry forward a shared "subject" (a course name/acronym
# mentioned once, e.g. "Operating Systems") into clauses that don't repeat
# it, so each resulting task objective is independently resolvable by
# whatever specialist agent handles it (see MockLLMProvider.plan_mission).
_CLAUSE_SPLIT_RE = re.compile(r",\s+and\s+|,\s+|\s+and\s+")
_SUBJECT_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+|[A-Z]{2,6}\d{0,3})\b")
_EXAM_ELIGIBILITY_KEYWORDS = (
    "eligib",
    "can i write",
    "can i take",
    "can i sit",
    "can i appear",
    "sit for",
    "write my",
    "appear for",
)
_EXAM_SCHEDULE_KEYWORDS = ("when", "schedule", "date")


def _title_acronym(title: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", title) if w.lower() not in _ACRONYM_STOPWORDS]
    return "".join(w[0] for w in words).upper()


def _extract_course_reference(query: str, enrolled_courses: List[CourseSummary]) -> Optional[str]:
    for course in enrolled_courses:
        if re.search(rf"\b{re.escape(course.course_code)}\b", query, re.IGNORECASE):
            return course.course_code
    for course in enrolled_courses:
        if course.title.lower() in query.lower():
            return course.title
    for course in enrolled_courses:
        acronym = _title_acronym(course.title)
        if acronym and re.search(rf"\b{re.escape(acronym)}\b", query, re.IGNORECASE):
            return acronym
    match = _COURSE_PHRASE_RE.search(query)
    if match:
        return match.group(1).strip()
    return None


def _split_goal_clauses(goal: str) -> List[str]:
    cleaned = goal.strip()
    if cleaned.endswith("."):
        cleaned = cleaned[:-1]
    parts = [p.strip() for p in _CLAUSE_SPLIT_RE.split(cleaned) if p.strip()]
    return parts or ([cleaned] if cleaned else [])


def _extract_shared_subject(goal: str) -> Optional[str]:
    match = _SUBJECT_RE.search(goal)
    return match.group(1) if match else None


def _classify(query: str) -> AcademicIntent:
    q = query.lower()
    if "exam" in q and any(keyword in q for keyword in _EXAM_ELIGIBILITY_KEYWORDS):
        return AcademicIntent.EXAM_ELIGIBILITY
    if ("how many classes" in q or "need to attend" in q) and "attend" in q:
        return AcademicIntent.ATTENDANCE_RECOVERY
    if "timetable" in q or "class schedule" in q or "schedule of classes" in q:
        return AcademicIntent.TIMETABLE
    if "exam" in q and any(keyword in q for keyword in _EXAM_SCHEDULE_KEYWORDS):
        return AcademicIntent.EXAM_SCHEDULE
    if "attendance" in q:
        return AcademicIntent.ATTENDANCE_STATUS
    if "polic" in q:
        return AcademicIntent.POLICY_QUESTION
    return AcademicIntent.UNKNOWN


class MockLLMProvider(LLMProvider):
    """Deterministic LLM stand-in -- no network, no model, fully reproducible."""

    name = "mock"

    def plan_mission(
        self, mission_id: str, goal: str, *, supported_agents: List[AgentName]
    ) -> MissionPlan:
        agent = AgentName.ACADEMIC_AGENT
        if agent not in supported_agents:
            agent = supported_agents[0] if supported_agents else AgentName.ACADEMIC_AGENT

        clauses = _split_goal_clauses(goal)
        subject = _extract_shared_subject(goal)

        tasks: List[MissionTask] = []
        for index, clause in enumerate(clauses, start=1):
            objective = clause
            if subject and subject.lower() not in objective.lower():
                objective = f"{objective} in {subject}"
            tasks.append(
                MissionTask(
                    task_id=f"{mission_id}-task-{index}",
                    mission_id=mission_id,
                    agent=agent,
                    objective=objective,
                    dependencies=[],
                    requires_evidence=True,
                )
            )
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=tasks)

    def classify_academic_intent(
        self, query: str, enrolled_courses: List[CourseSummary]
    ) -> AcademicIntentResult:
        return AcademicIntentResult(
            intent=_classify(query),
            raw_course_reference=_extract_course_reference(query, enrolled_courses),
        )

    def generate_academic_response(self, context: AcademicResponseContext) -> str:
        if context.verification_status == VerificationStatus.FAILED:
            return self._render_failed(context)
        course_dependent_intent = context.intent in (
            AcademicIntent.ATTENDANCE_STATUS,
            AcademicIntent.ATTENDANCE_RECOVERY,
            AcademicIntent.EXAM_ELIGIBILITY,
        )
        if (
            course_dependent_intent
            and context.course_resolution is not None
            and context.course_resolution.status == CourseResolutionStatus.AMBIGUOUS
        ):
            options = ", ".join(context.course_resolution.candidates)
            return (
                f"Your reference to '{context.course_resolution.query_reference}' matches more than "
                f"one of your courses ({options}). Please specify which one you mean."
            )

        hedge = ""
        if context.verification_status == VerificationStatus.NEEDS_REVIEW:
            hedge = "Note: I couldn't fully confirm this, so treat it as provisional. "

        if context.intent in (
            AcademicIntent.ATTENDANCE_STATUS,
            AcademicIntent.ATTENDANCE_RECOVERY,
            AcademicIntent.EXAM_ELIGIBILITY,
        ):
            return hedge + self._render_attendance(context)
        if context.intent == AcademicIntent.TIMETABLE:
            return hedge + self._render_timetable(context)
        if context.intent == AcademicIntent.EXAM_SCHEDULE:
            return hedge + self._render_exam_schedule(context)
        if context.intent == AcademicIntent.POLICY_QUESTION:
            return hedge + self._render_policy(context)
        return (
            "I can help with attendance, exam eligibility, timetable, or exam schedule "
            "questions -- I couldn't tell what you're asking for here."
        )

    def _render_failed(self, context: AcademicResponseContext) -> str:
        if context.student_name is None:
            return "I couldn't find a student record matching that request, so I can't answer this."
        if (
            context.course_resolution is not None
            and context.course_resolution.status == CourseResolutionStatus.UNKNOWN
        ):
            ref = context.course_resolution.query_reference or "that course"
            return f"I couldn't find '{ref}' among your enrolled courses, so I can't answer this."
        if context.intent == AcademicIntent.UNKNOWN:
            return (
                "I can help with attendance, exam eligibility, timetable, or exam schedule "
                "questions -- I couldn't tell what you're asking for here."
            )
        if context.errors:
            return "I couldn't complete this request: " + "; ".join(context.errors)
        return "I couldn't complete this request due to a data problem; please try again or contact the Academic Office."

    def _render_attendance(self, context: AcademicResponseContext) -> str:
        att = context.attendance
        course = context.course
        if att is None or att.status != AttendanceRuleStatus.OK:
            if att is not None and att.status == AttendanceRuleStatus.NO_CLASSES_CONDUCTED:
                name = course.title if course else "that course"
                return f"No classes have been conducted yet for {name}, so attendance can't be calculated."
            return "I don't have enough verified information to answer that attendance question right now."

        course_name = course.title if course else "that course"
        course_code = course.course_code if course else ""
        lines = [
            f"Your attendance in {course_name} ({course_code}) is {att.current_percentage}% "
            f"({att.classes_attended}/{att.classes_conducted} classes), against a required "
            f"{att.required_percentage}%."
        ]
        if att.eligible_now:
            lines.append("You currently meet the attendance requirement.")
            if att.maximum_additional_absences_allowed is not None:
                lines.append(
                    f"You could miss up to {att.maximum_additional_absences_allowed} more classes "
                    "(assuming they're conducted) and still stay at or above the requirement."
                )
        else:
            if att.threshold_reachable and att.classes_needed_to_reach_threshold is not None:
                lines.append(
                    f"You need to attend at least {att.classes_needed_to_reach_threshold} more "
                    f"consecutive classes (attending all of them) to reach {att.required_percentage}%."
                )
            elif att.threshold_reachable is False:
                lines.append(
                    "Based on the classes already conducted, this requirement can no longer be "
                    "reached this semester."
                )

        if context.threshold is not None and context.threshold.document_id:
            lines.append(
                f"Source: {context.threshold.document_id} ({context.threshold.policy_version}), "
                f"section '{context.threshold.section}'."
            )

        if context.eligibility is not None:
            if context.eligibility.status == ExamEligibilityStatus.ELIGIBLE:
                lines.append(
                    f"Based on attendance alone, you are eligible to appear for the exam. "
                    f"{context.eligibility.caveat}"
                )
            elif context.eligibility.status == ExamEligibilityStatus.NOT_ELIGIBLE:
                lines.append(
                    f"Based on attendance alone, you are NOT currently eligible to appear for the exam. "
                    f"{context.eligibility.caveat}"
                )

        return " ".join(lines)

    def _render_timetable(self, context: AcademicResponseContext) -> str:
        if not context.timetable:
            return "I couldn't find any timetable entries for your current enrollments."
        lines = ["Your timetable:"]
        for entry in sorted(context.timetable, key=lambda e: (e.weekday, e.start_time)):
            lines.append(
                f"- {_WEEKDAY_NAMES[entry.weekday]} {entry.start_time}-{entry.end_time}: "
                f"{entry.course_title} ({entry.course_code}) at {entry.location}"
            )
        return "\n".join(lines)

    def _render_exam_schedule(self, context: AcademicResponseContext) -> str:
        if not context.exams:
            return "I couldn't find any scheduled exams for your current enrollments."
        lines = ["Your exam schedule:"]
        for entry in sorted(context.exams, key=lambda e: e.scheduled_start):
            lines.append(
                f"- {entry.course_title} ({entry.course_code}) {entry.exam_type}: "
                f"{entry.scheduled_start} at {entry.location}"
            )
        return "\n".join(lines)

    def _render_policy(self, context: AcademicResponseContext) -> str:
        if not context.evidence:
            return "I couldn't find any policy evidence covering that question, so I can't answer it reliably."
        lines = ["Here's what the policy documents say:"]
        for ev in context.evidence[:3]:
            lines.append(f"- ({ev.document_id}, {ev.section}): {ev.snippet}")
        return "\n".join(lines)
