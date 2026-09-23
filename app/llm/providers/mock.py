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
from app.schemas.career import CareerIntent, CareerIntentResult, CareerResponseContext, OpportunityEligibilityStatus
from app.schemas.enums import AgentName, VerificationStatus
from app.schemas.events import EventsIntent, EventsIntentResult, EventsResponseContext
from app.schemas.mission import MissionPlan, MissionTask
from app.schemas.services import ServicesIntent, ServicesIntentResult, ServicesResponseContext

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


_CAREER_PREP_AGENTS = frozenset(
    {AgentName.ACADEMIC_AGENT, AgentName.CAREER_AGENT, AgentName.EVENTS_OPPORTUNITY_AGENT}
)


def _is_career_prep_goal(goal: str) -> bool:
    q = goal.lower()
    return "internship" in q and "skill gap" in q and ("workshop" in q or "event" in q)


def _is_campus_services_goal(goal: str) -> bool:
    q = goal.lower()
    return ("complaint" in q or "grievance" in q) and ("case" in q or "overdue" in q or "sla" in q or "procedure" in q)


# Phase 7 action-mission heuristics: the demo/tests embed the structured
# values a real LLM would extract (an event title, a calendar entry title, a
# complaint description) as single-quoted segments in the goal text -- this
# mock only needs to be a deterministic stand-in, not a real NL parser, and
# quoting keeps extraction unambiguous rather than guessing at free text.
_QUOTED_RE = re.compile(r"'([^']+)'")
_CASE_CATEGORY_KEYWORDS = [
    ("hostel", "hostel"),
    ("wifi", "it_helpdesk"),
    ("internet", "it_helpdesk"),
    ("laptop", "it_helpdesk"),
    ("portal", "it_helpdesk"),
    ("fee", "fees"),
    ("facilit", "facilities"),
    ("projector", "facilities"),
    ("transcript", "administrative"),
    ("certificate", "administrative"),
    ("bonafide", "administrative"),
]


def _is_event_registration_goal(goal: str) -> bool:
    q = goal.lower()
    return "register" in q and any(k in q for k in ("workshop", "event", "talk")) and bool(_QUOTED_RE.search(goal))


def _is_calendar_creation_goal(goal: str) -> bool:
    q = goal.lower()
    return "calendar" in q and len(_QUOTED_RE.findall(goal)) >= 2


def _is_case_creation_goal(goal: str) -> bool:
    q = goal.lower()
    has_intent = any(k in q for k in ("file", "submit", "raise")) and ("complaint" in q or "case" in q)
    return has_intent and bool(_QUOTED_RE.search(goal)) and _infer_case_category(q) is not None


def _infer_case_category(goal_lower: str) -> Optional[str]:
    for keyword, category in _CASE_CATEGORY_KEYWORDS:
        if keyword in goal_lower:
            return category
    return None


def _infer_case_priority(goal_lower: str) -> str:
    if "urgent" in goal_lower:
        return "urgent"
    if "high priority" in goal_lower or "high-priority" in goal_lower:
        return "high"
    return "normal"


def _build_event_registration_plan(mission_id: str, goal: str) -> MissionPlan:
    event_title = _QUOTED_RE.search(goal).group(1)
    discovery_task = MissionTask(
        task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.EVENTS_OPPORTUNITY_AGENT,
        objective=f"Find the '{event_title}' workshop and check for schedule conflicts with my classes and exams",
        dependencies=[], requires_evidence=True,
    )
    action_task = MissionTask(
        task_id=f"{mission_id}-task-2", mission_id=mission_id, agent=AgentName.ACTION_AGENT,
        objective=f"Register for '{event_title}'", dependencies=[discovery_task.task_id],
        constraints={"tool_name": "register_event", "event_title": event_title},
    )
    return MissionPlan(mission_id=mission_id, goal=goal, tasks=[discovery_task, action_task])


def _build_calendar_creation_plan(mission_id: str, goal: str) -> MissionPlan:
    titles = _QUOTED_RE.findall(goal)
    entry_title, source_event_title = titles[0], titles[1]
    action_task = MissionTask(
        task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.ACTION_AGENT,
        objective=f"Create a personal calendar entry '{entry_title}'", dependencies=[],
        constraints={
            "tool_name": "create_calendar_event",
            "title": entry_title,
            "source_event_title": source_event_title,
            "lead_time_hours": 24,
            "duration_hours": 1,
        },
    )
    return MissionPlan(mission_id=mission_id, goal=goal, tasks=[action_task])


def _build_case_creation_plan(mission_id: str, goal: str) -> MissionPlan:
    # Deliberately a standalone Action Agent task, not chained after a
    # CampusServicesAgent read task: a newly-filed case has no CaseSLA row
    # yet (that's set up by whatever downstream process assigns/tracks it,
    # out of scope here), which ServicesVerifier -- correctly, per its own
    # existing Phase 6 rule -- flags NEEDS_REVIEW ("SLA data missing for
    # case(s)"). Chaining would make an unrelated case-creation mission's
    # pause depend on how many *other* cases the student already has, which
    # is real but orthogonal to this action. The "use Campus Services to
    # prepare the submission" collaboration is instead demonstrated by
    # scripts/demo_actions.py calling the read path directly beforehand.
    q = goal.lower()
    description_text = _QUOTED_RE.search(goal).group(1)
    category = _infer_case_category(q) or "administrative"
    priority = _infer_case_priority(q)
    action_task = MissionTask(
        task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.ACTION_AGENT,
        objective=f"File a {category} complaint: {description_text}", dependencies=[],
        constraints={
            "tool_name": "create_campus_case",
            "category": category,
            "description": description_text,
            "priority": priority,
        },
    )
    return MissionPlan(mission_id=mission_id, goal=goal, tasks=[action_task])


def _build_career_prep_plan(mission_id: str, goal: str) -> MissionPlan:
    """The Phase 6 flagship mission: Academic timetable/exams (independent) +
    Career opportunity/skill-gap discovery (independent), both feeding into
    an Events Agent task that can only run once all three complete (see
    app/graph/dispatcher.py's dependency-fact propagation)."""
    timetable_task = MissionTask(
        task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT,
        objective="What is my timetable?", dependencies=[], requires_evidence=False,
    )
    exam_task = MissionTask(
        task_id=f"{mission_id}-task-2", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT,
        objective="When are my exams?", dependencies=[], requires_evidence=False,
    )
    career_task = MissionTask(
        task_id=f"{mission_id}-task-3", mission_id=mission_id, agent=AgentName.CAREER_AGENT,
        objective="Find internships I'm eligible for related to AI and identify my skill gaps",
        dependencies=[], requires_evidence=True,
    )
    events_task = MissionTask(
        task_id=f"{mission_id}-task-4", mission_id=mission_id, agent=AgentName.EVENTS_OPPORTUNITY_AGENT,
        objective="Find workshops related to AI and my skill gaps that don't conflict with my classes or exams",
        dependencies=[timetable_task.task_id, exam_task.task_id, career_task.task_id], requires_evidence=True,
    )
    return MissionPlan(mission_id=mission_id, goal=goal, tasks=[timetable_task, exam_task, career_task, events_task])


def _build_campus_services_plan(mission_id: str, goal: str) -> MissionPlan:
    task = MissionTask(
        task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.CAMPUS_SERVICES_AGENT,
        objective=goal, dependencies=[], requires_evidence=True,
    )
    return MissionPlan(mission_id=mission_id, goal=goal, tasks=[task])


def _classify_career(query: str) -> CareerIntent:
    q = query.lower()
    if any(k in q for k in ("status of my application", "application status", "my application")):
        return CareerIntent.APPLICATION_STATUS
    if any(k in q for k in ("internship", "job", "opportunit", "eligib", "skill gap", "skill-gap")):
        return CareerIntent.OPPORTUNITY_DISCOVERY
    if "polic" in q:
        return CareerIntent.POLICY_QUESTION
    return CareerIntent.UNKNOWN


def _classify_events(query: str) -> EventsIntent:
    q = query.lower()
    if any(k in q for k in ("am i registered", "my registration", "registration status")):
        return EventsIntent.REGISTRATION_STATUS
    if any(k in q for k in ("workshop", "event", "conflict")):
        return EventsIntent.EVENT_DISCOVERY
    if "polic" in q:
        return EventsIntent.POLICY_QUESTION
    return EventsIntent.UNKNOWN


def _classify_services(query: str) -> ServicesIntent:
    q = query.lower()
    if any(k in q for k in ("complaint", "grievance", "case", "overdue", "sla")):
        return ServicesIntent.CASE_STATUS
    if "polic" in q or "procedure" in q:
        return ServicesIntent.POLICY_QUESTION
    return ServicesIntent.UNKNOWN


class MockLLMProvider(LLMProvider):
    """Deterministic LLM stand-in -- no network, no model, fully reproducible."""

    name = "mock"

    def plan_mission(
        self, mission_id: str, goal: str, *, supported_agents: List[AgentName]
    ) -> MissionPlan:
        # Pattern-matched multi-agent planning paths for the two Phase 6
        # flagship goal shapes, tried first (and only used if every agent
        # they need is actually registered) -- kept *alongside*, never
        # replacing, the generic single-agent clause-splitting fallback
        # below, so existing academic-only missions are unaffected. A real
        # LLM provider would instead reason about this directly from the
        # tool schema already built in Phase 5; this mock only needs to be
        # a good enough deterministic stand-in for tests/demo.
        supported = set(supported_agents)
        # More specific action-mission patterns are tried before the more
        # general read-only campus-services pattern -- a "file a complaint"
        # goal would otherwise also satisfy _is_campus_services_goal's looser
        # keyword check and get silently routed to the wrong (read-only) plan.
        if _is_event_registration_goal(goal) and {AgentName.EVENTS_OPPORTUNITY_AGENT, AgentName.ACTION_AGENT}.issubset(supported):
            return _build_event_registration_plan(mission_id, goal)
        if _is_calendar_creation_goal(goal) and AgentName.ACTION_AGENT in supported:
            return _build_calendar_creation_plan(mission_id, goal)
        if _is_case_creation_goal(goal) and AgentName.ACTION_AGENT in supported:
            return _build_case_creation_plan(mission_id, goal)
        if _is_career_prep_goal(goal) and _CAREER_PREP_AGENTS.issubset(supported):
            return _build_career_prep_plan(mission_id, goal)
        if _is_campus_services_goal(goal) and AgentName.CAMPUS_SERVICES_AGENT in supported:
            return _build_campus_services_plan(mission_id, goal)

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

    # ------------------------------------------------------------------
    # Career Agent
    # ------------------------------------------------------------------

    def classify_career_intent(self, query: str) -> CareerIntentResult:
        return CareerIntentResult(intent=_classify_career(query))

    def generate_career_response(self, context: CareerResponseContext) -> str:
        if context.verification_status == VerificationStatus.FAILED:
            if context.student_name is None:
                return "I couldn't find a student record matching that request, so I can't answer this."
            if context.intent == CareerIntent.UNKNOWN:
                return (
                    "I can help with internship/job eligibility, application status, or career policy "
                    "questions -- I couldn't tell what you're asking for here."
                )
            return "I couldn't complete this request: " + "; ".join(context.errors or ["a data problem occurred"])

        hedge = "Note: I couldn't fully confirm this, so treat it as provisional. " if context.verification_status == VerificationStatus.NEEDS_REVIEW else ""
        if context.intent == CareerIntent.OPPORTUNITY_DISCOVERY:
            return hedge + self._render_opportunities(context)
        if context.intent == CareerIntent.APPLICATION_STATUS:
            return hedge + self._render_applications(context)
        if context.intent == CareerIntent.POLICY_QUESTION:
            return hedge + self._render_career_policy(context)
        return (
            "I can help with internship/job eligibility, application status, or career policy "
            "questions -- I couldn't tell what you're asking for here."
        )

    def _render_opportunities(self, context: CareerResponseContext) -> str:
        eligible = [e for e in context.eligibilities if e.status == OpportunityEligibilityStatus.ELIGIBLE]
        ineligible = [e for e in context.eligibilities if e.status != OpportunityEligibilityStatus.ELIGIBLE]
        lines: List[str] = []

        if eligible:
            lines.append(f"You are eligible for {len(eligible)} opportunit{'y' if len(eligible) == 1 else 'ies'}:")
            for e in eligible:
                applied_note = (
                    f" (you already applied -- status: {e.application_status})" if e.already_applied else ""
                )
                lines.append(
                    f"- {e.opportunity.title} at {e.opportunity.company} "
                    f"(min CGPA {e.opportunity.minimum_cgpa}){applied_note}"
                )
        else:
            lines.append("No open opportunities matched your eligibility criteria right now.")

        if ineligible:
            lines.append(
                f"{len(ineligible)} opportunit{'y' if len(ineligible) == 1 else 'ies'} you're not currently "
                "eligible for, for example:"
            )
            for e in ineligible[:3]:
                lines.append(f"- {e.opportunity.title}: {'; '.join(e.blocking_reasons)}")

        if context.skill_gaps:
            lines.append(f"Skill gaps worth developing: {', '.join(context.skill_gaps)}.")

        return "\n".join(lines)

    def _render_applications(self, context: CareerResponseContext) -> str:
        if not context.applications:
            return "You have no recorded applications."
        lines = ["Your application status:"]
        for app in context.applications:
            lines.append(f"- {app.opportunity_title}: {app.status}")
        return "\n".join(lines)

    def _render_career_policy(self, context: CareerResponseContext) -> str:
        if not context.evidence:
            return "I couldn't find any policy evidence covering that question, so I can't answer it reliably."
        lines = ["Here's what the policy documents say:"]
        for ev in context.evidence[:3]:
            lines.append(f"- ({ev.document_id}, {ev.section}): {ev.snippet}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Events & Opportunity Agent
    # ------------------------------------------------------------------

    def classify_events_intent(self, query: str) -> EventsIntentResult:
        return EventsIntentResult(intent=_classify_events(query))

    def generate_events_response(self, context: EventsResponseContext) -> str:
        if context.verification_status == VerificationStatus.FAILED:
            if context.student_name is None:
                return "I couldn't find a student record matching that request, so I can't answer this."
            if context.intent == EventsIntent.UNKNOWN:
                return (
                    "I can help with finding events/workshops, checking your registrations, or events "
                    "policy questions -- I couldn't tell what you're asking for here."
                )
            return "I couldn't complete this request: " + "; ".join(context.errors or ["a data problem occurred"])

        hedge = "Note: I couldn't fully confirm this, so treat it as provisional. " if context.verification_status == VerificationStatus.NEEDS_REVIEW else ""
        if context.intent in (EventsIntent.EVENT_DISCOVERY, EventsIntent.REGISTRATION_STATUS):
            return hedge + self._render_event_assessments(context)
        if context.intent == EventsIntent.POLICY_QUESTION:
            return hedge + self._render_events_policy(context)
        return (
            "I can help with finding events/workshops, checking your registrations, or events policy "
            "questions -- I couldn't tell what you're asking for here."
        )

    def _render_event_assessments(self, context: EventsResponseContext) -> str:
        if not context.assessments:
            return "I couldn't find any relevant upcoming events."

        conflict_free = [a for a in context.assessments if not a.timetable_conflicts and not a.exam_conflicts]
        conflicted = [a for a in context.assessments if a.timetable_conflicts or a.exam_conflicts]
        lines: List[str] = []

        if conflict_free:
            lines.append("Events with no schedule conflicts:")
            for a in conflict_free:
                if a.already_registered:
                    note = f" (you're already registered -- {a.registration_status})"
                else:
                    note = f" ({a.availability.value})"
                lines.append(f"- {a.event.title} on {a.event.start_at.isoformat()} at {a.event.location}{note}")

        if conflicted:
            lines.append("Events that conflict with your academic schedule:")
            for a in conflicted:
                reasons = [f"class {c.course_code}" for c in a.timetable_conflicts]
                reasons += [f"{c.course_code} exam" for c in a.exam_conflicts]
                lines.append(f"- {a.event.title}: conflicts with {', '.join(reasons)}")

        return "\n".join(lines)

    def _render_events_policy(self, context: EventsResponseContext) -> str:
        if not context.evidence:
            return "I couldn't find any policy evidence covering that question, so I can't answer it reliably."
        lines = ["Here's what the policy documents say:"]
        for ev in context.evidence[:3]:
            lines.append(f"- ({ev.document_id}, {ev.section}): {ev.snippet}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Campus Services Agent
    # ------------------------------------------------------------------

    def classify_services_intent(self, query: str) -> ServicesIntentResult:
        return ServicesIntentResult(intent=_classify_services(query))

    def generate_services_response(self, context: ServicesResponseContext) -> str:
        if context.verification_status == VerificationStatus.FAILED:
            if context.student_name is None:
                return "I couldn't find a student record matching that request, so I can't answer this."
            if context.intent == ServicesIntent.UNKNOWN:
                return (
                    "I can help with checking the status of your campus service cases, or campus "
                    "services policy questions -- I couldn't tell what you're asking for here."
                )
            return "I couldn't complete this request: " + "; ".join(context.errors or ["a data problem occurred"])

        hedge = "Note: I couldn't fully confirm this, so treat it as provisional. " if context.verification_status == VerificationStatus.NEEDS_REVIEW else ""
        if context.intent == ServicesIntent.CASE_STATUS:
            return hedge + self._render_case_assessments(context)
        if context.intent == ServicesIntent.POLICY_QUESTION:
            return hedge + self._render_services_policy(context)
        return (
            "I can help with checking the status of your campus service cases, or campus services "
            "policy questions -- I couldn't tell what you're asking for here."
        )

    def _render_case_assessments(self, context: ServicesResponseContext) -> str:
        if not context.case_assessments:
            return "You have no recorded campus service cases."

        lines = ["Your campus service cases:"]
        overdue = []
        for a in context.case_assessments:
            breach_note = ""
            if a.response_breached and a.resolution_breached:
                breach_note = " -- OVERDUE (response and resolution both past due)"
            elif a.resolution_breached:
                breach_note = " -- OVERDUE (resolution past due)"
            elif a.response_breached:
                breach_note = " -- response past due"
            lines.append(f"- {a.case.case_code} ({a.case.category}, {a.case.priority}, {a.case.status}){breach_note}")
            if a.response_breached or a.resolution_breached:
                overdue.append(a.case.case_code)

        if overdue:
            lines.append(f"Overdue case(s) requiring attention: {', '.join(overdue)}.")
        else:
            lines.append("No cases are currently overdue on their SLA.")

        if context.evidence:
            lines.append(f"Source: {context.evidence[0].document_id}, section '{context.evidence[0].section}'.")

        return "\n".join(lines)

    def _render_services_policy(self, context: ServicesResponseContext) -> str:
        if not context.evidence:
            return "I couldn't find any policy evidence covering that question, so I can't answer it reliably."
        lines = ["Here's what the policy documents say:"]
        for ev in context.evidence[:3]:
            lines.append(f"- ({ev.document_id}, {ev.section}): {ev.snippet}")
        return "\n".join(lines)
