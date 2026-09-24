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
from app.schemas.agent_chat import EnquiryConsultation, EnquiryPlan
from app.schemas.career import CareerIntent, CareerIntentResult, CareerResponseContext, OpportunityEligibilityStatus
from app.schemas.enums import AgentName, VerificationStatus
from app.schemas.events import EventsIntent, EventsIntentResult, EventsResponseContext
from app.schemas.faculty import FacultyQueryPlan
from app.schemas.department import HodQueryPlan
from app.schemas.mission import MissionPlan, MissionTask
from app.schemas.services import ServicesIntent, ServicesIntentResult, ServicesResponseContext
from app.schemas.workflow import PermissionIntent

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


_SUBJECT_LEADING_WORDS = {"My", "The", "Our", "Check", "Can", "Is", "Am", "What", "When", "How"}


def _extract_shared_subject(goal: str) -> Optional[str]:
    match = _SUBJECT_RE.search(goal)
    if not match:
        return None
    # A sentence-initial capitalised word ("My Operating Systems ...") is not
    # part of the course name.
    words = match.group(1).split()
    while len(words) > 1 and words[0] in _SUBJECT_LEADING_WORDS:
        words = words[1:]
    return " ".join(words)


_RECOVERY_PHRASES = ("how many classes", "how many more classes", "need to attend", "classes do i need")
_RECOVERY_WORDS = ("recover", "catch up", "make up my attendance", "improve my attendance")
# Reaching/raising the threshold is recovery, not a status lookup
# (app.schemas.academic.ACADEMIC_INTENT_DEFINITIONS) -- only when the query
# is about attendance at all.
_RECOVERY_TARGET_CUES = ("reach", "get to", "get back", "bring", "raise")


def _classify(query: str) -> AcademicIntent:
    q = query.lower()
    if "exam" in q and any(keyword in q for keyword in _EXAM_ELIGIBILITY_KEYWORDS):
        return AcademicIntent.EXAM_ELIGIBILITY
    recovery_phrase = any(phrase in q for phrase in _RECOVERY_PHRASES) and ("attend" in q or "need" in q)
    recovery_target = "attend" in q and any(cue in q for cue in _RECOVERY_TARGET_CUES)
    if recovery_phrase or recovery_target or any(word in q for word in _RECOVERY_WORDS):
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
    if "internship" not in q:
        return False
    if "skill gap" in q and ("workshop" in q or "event" in q):
        return True
    # "Prepare for AI internships without missing classes" -- preparing for
    # internships around the student's timetable is the same Academic +
    # Career -> Events collaboration, just phrased without naming each step.
    return "prepar" in q or ("workshop" in q and "skill" in q)


# Generic domain vocabulary for the fallback router below: a goal is routed to
# the specialist whose domain it mentions, and is declined outright (with an
# explanation) when it mentions none -- rather than being forced onto the
# Academic Agent, which would answer an unrelated goal with a confusing
# "couldn't find that course" message.
_EVENTS_WORDS = ("workshop", "event", "hackathon", "seminar", "club", "competition", "contest", "webinar", "bootcamp")
_CAREER_WORDS = ("internship", "job", "placement", "career", "resume", "recruit", "opportunit")
_SERVICES_WORDS = ("complaint", "grievance", "hostel", "ticket", "sla", "helpdesk", "my cases", "my case")
_ACADEMIC_WORDS = (
    "attend", "exam", "class", "course", "timetable", "grade", "gpa", "marks", "schedule", "semester",
    "polic", "eligib", "lecture", "credit", "subject", "academic", "syllabus",
)
_CONFLICT_WORDS = ("clash", "conflict", "without missing", "fit my", "around my", "don't miss", "schedule", "timetable")
_REGISTRATION_WORDS = ("regist", "sign me up", "sign up", "enrol me", "enroll me")

# Objectives built only from app/agents/events/agent.py's relevance
# stopwords, so the Events Agent filters by upstream skill gaps (or not at
# all) rather than by incidental filler words in the student's phrasing.
_EVENTS_DISCOVERY_OBJECTIVE = "Find events and workshops"
_EVENTS_SKILL_OBJECTIVE = "Find workshops related to my skill gaps"
_NO_CONFLICT_SUFFIX = " that don't conflict with my classes and exams"

_UNSUPPORTED_GOAL_NOTE = (
    "This goal doesn't match anything CampusNexus can do. Supported: academics (attendance, exam eligibility, "
    "timetable, exam schedule, academic policy), careers (eligible internships, skill gaps, application status), "
    "campus events and workshops, campus complaints and their SLA status, and approval-gated actions "
    "(event registration, personal calendar entries, filing a campus complaint)."
)
_UNNAMED_EVENT_NOTE = (
    "Registration was not prepared: no specific event was named. Select one of the conflict-free candidate "
    "events (or ask to register for it by its exact title), and the registration will be prepared for approval."
)


def _mentions(goal_lower: str, words: tuple) -> bool:
    return any(word in goal_lower for word in words)


# Phase 16: live-class questions ("has my class started?", "was I marked present?").
_LIVE_CLASS_PHRASES = (
    "class started", "class begun", "class begin", "class start", "class on now", "attendance being taken",
    "taking attendance", "attendance taken", "marked present", "marked me", "was i marked", "am i marked",
    "class is happening", "class happening", "class now", "class is now", "next class", "class is next", "class next",
    "current class",
)
_PERMISSION_EVENT_WORDS = ("contest", "workshop", "hackathon", "event", "fest", "competition", "seminar", "talk",
                           "expo", "summit", "exhibition", "meet", "camp", "fair", "night")
_EVENT_REF_RE = re.compile(
    r"(?:attend|participate in|join|for|in)\s+(?:the\s+)?([a-z0-9][a-z0-9 &'-]*?)(?:\s+(?:on|tomorrow|today|next|this|because|since|as)\b|[.,!?]|$)"
)


def _event_reference(q: str) -> Optional[str]:
    for match in _EVENT_REF_RE.finditer(q):
        phrase = match.group(1).strip()
        if any(word in phrase for word in _PERMISSION_EVENT_WORDS):
            return phrase
    return None


def _permission_intent(message: str) -> PermissionIntent:
    q = message.lower().strip()
    if any(k in q for k in (" od ", " od.", "on duty", "on-duty")) or q.startswith("od "):
        request_type = "od_request"
    elif "substitut" in q:
        request_type = "class_substitution"
    elif "department permission" in q or "permission from the department" in q:
        request_type = "department_permission"
    elif "leave" in q:
        request_type = "leave_request"
    elif any(k in q for k in ("was absent", "missed", "i was sick", "attendance permission", "excuse my absence", "absent yesterday")):
        request_type = "attendance_permission"
    elif any(w in q for w in _PERMISSION_EVENT_WORDS) and any(k in q for k in ("permission", "attend", "participate", "join")):
        request_type = "event_permission"
    else:
        request_type = "unclear"
    date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", q)
    if date_match:
        date_reference, specific = "specific_date", date_match.group(1)
    else:
        specific = None
        date_reference = next((d for d in ("yesterday", "tomorrow", "today") if d in q), "none")
    day_part = "afternoon" if "afternoon" in q else "morning" if "morning" in q else "full_day"
    reason_match = re.search(r"\b(?:because|since|as)\s+(?:of\s+)?(.+?)(?:[.!?](?:\s|$)|$)", message.strip(), flags=re.IGNORECASE)
    event_reference = _event_reference(q) if request_type in ("event_permission", "od_request") else None
    return PermissionIntent(
        request_type=request_type, event_reference=event_reference, date_reference=date_reference,
        specific_date=specific, day_part=day_part, reason=reason_match.group(1).strip() if reason_match else None,
    )


_MAKE_REQUEST_PHRASES = (
    "i need leave", "need leave", "i want leave", "apply for leave", "request leave", "take leave", "leave tomorrow",
    "leave today", "substitute", "substitution", "on duty", "on-duty", "i need permission", "can i get permission",
)


def _hod_plan(message: str) -> HodQueryPlan:
    q = message.lower()
    section = re.search(r"\bsection\s*([a-z0-9]{1,3})\b", q)
    year = re.search(r"\b([1-6])(?:st|nd|rd|th)\s+year\b", q)
    if any(k in q for k in _MAKE_REQUEST_PHRASES):
        intent = "make_request"
    elif any(k in q for k in ("complaint", "sla", "grievance")):
        intent = "complaints_breached"
    elif "escalat" in q:
        intent = "escalated_student_requests"
    elif "request" in q:
        intent = "pending_faculty_requests"
    elif any(k in q for k in ("normally", "everything", "overview", "how is the department", "anything wrong")):
        intent = "department_overview"
    elif any(k in q for k in ("haven't started", "havent started", "not started", "didn't start", "did not start",
                              "not start on time", "delayed", "late", "start on time")):
        intent = "classes_not_started"
    elif "lowest" in q:
        intent = "lowest_course_attendance"
    elif "below" in q or "%" in q or "threshold" in q or "shortage" in q:
        intent = "below_threshold"
    elif "present" in q:
        intent = "section_present_today"
    elif any(k in q for k in ("running", "right now", "in session", "happening now")):
        intent = "classes_running"
    elif "faculty" in q and ("teach" in q or "today" in q):
        intent = "faculty_teaching_today"
    elif "event" in q:
        intent = "department_events"
    elif "class" in q and ("today" in q or "how many" in q):
        intent = "classes_today"
    else:
        intent = "unknown"
    return HodQueryPlan(
        intent=intent, section_reference=section.group(1).upper() if section else None,
        year_reference=int(year.group(1)) if year else None,
    )


def _faculty_plan(message: str) -> FacultyQueryPlan:
    q = message.lower()
    if any(k in q for k in _MAKE_REQUEST_PHRASES):
        return FacultyQueryPlan(intent="make_request")
    if "request" in q or "pending" in q or "permission" in q or "waiting for me" in q:
        intent = "pending_requests"
    elif "below" in q or "%" in q or "shortage" in q or "less than" in q:
        intent = "below_threshold"
    elif "absent" in q or "not marked" in q or "who is missing" in q:
        intent = "absent_students"
    elif "present" in q or "how many students" in q:
        intent = "class_attendance"
    elif "next class" in q or "class is next" in q:
        intent = "next_class"
    elif any(k in q for k in ("happening", "right now", "current class", "class now", "started", "in session")):
        intent = "current_class"
    elif "class" in q and ("today" in q or "how many" in q or "schedule" in q):
        intent = "classes_today"
    else:
        intent = "unknown"
    course = re.search(r"\bmy\s+([a-z][a-z0-9 &]*?)\s+(?:class|course|section)\b", q)
    code = re.search(r"\b([a-z]{2}\d{3})\b", q)
    section = re.search(r"\bsection\s*([a-z0-9]{1,3})\b", q)
    year = re.search(r"\b([1-6])(?:st|nd|rd|th)\s+year\b", q)
    reference = code.group(1).upper() if code else (course.group(1).strip() if course else None)
    if reference in {"current", "next", "today's", "todays"}:
        reference = None
    if reference and reference.lower() not in q:
        reference = None
    return FacultyQueryPlan(
        intent=intent, course_reference=message[q.find(reference.lower()):q.find(reference.lower()) + len(reference)] if reference else None,
        section_reference=section.group(1).upper() if section else None, year_reference=int(year.group(1)) if year else None,
    )


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


def _build_event_registration_plan(mission_id: str, goal: str, *, with_schedule: bool) -> MissionPlan:
    """Events discovery, Academic timetable and Academic exams run in parallel
    (none needs another's output); the registration proposal depends on all
    three, so its pre-approval check sees the verified timetable/exams and
    can detect a conflict *before* a human is asked. ``with_schedule`` is
    False only when no Academic Agent is registered -- the proposal then
    honestly reports "conflicts not checked" (NEEDS_REVIEW) instead."""
    event_title = _QUOTED_RE.search(goal).group(1)
    tasks = [
        MissionTask(
            task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.EVENTS_OPPORTUNITY_AGENT,
            objective=f"Find the '{event_title}' event", dependencies=[], requires_evidence=True,
        )
    ]
    if with_schedule:
        tasks.append(
            MissionTask(
                task_id=f"{mission_id}-task-2", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT,
                objective="What is my timetable?", dependencies=[], requires_evidence=False,
            )
        )
        tasks.append(
            MissionTask(
                task_id=f"{mission_id}-task-3", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT,
                objective="When are my exams?", dependencies=[], requires_evidence=False,
            )
        )
    tasks.append(
        MissionTask(
            task_id=f"{mission_id}-task-{len(tasks) + 1}", mission_id=mission_id, agent=AgentName.ACTION_AGENT,
            objective=f"Register for '{event_title}'", dependencies=[t.task_id for t in tasks],
            constraints={"tool_name": "register_event", "event_title": event_title},
        )
    )
    return MissionPlan(mission_id=mission_id, goal=goal, tasks=tasks)


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


def _build_events_plan(
    mission_id: str,
    goal: str,
    *,
    with_career: bool,
    with_schedule: bool,
    unsupported_requests: Optional[List[str]] = None,
    selection_required_actions: Optional[List[str]] = None,
) -> MissionPlan:
    """Events discovery, optionally fed by Career skill gaps and/or Academic
    timetable + exam facts (the only way the Events Agent can check clashes)."""
    tasks: List[MissionTask] = []

    def _add(agent: AgentName, objective: str, *, deps: Optional[List[str]] = None, evidence: bool = True) -> str:
        task_id = f"{mission_id}-task-{len(tasks) + 1}"
        tasks.append(
            MissionTask(
                task_id=task_id, mission_id=mission_id, agent=agent, objective=objective,
                dependencies=list(deps or []), requires_evidence=evidence,
            )
        )
        return task_id

    upstream: List[str] = []
    if with_schedule:
        upstream.append(_add(AgentName.ACADEMIC_AGENT, "What is my timetable?", evidence=False))
        upstream.append(_add(AgentName.ACADEMIC_AGENT, "When are my exams?", evidence=False))
    if with_career:
        upstream.append(_add(AgentName.CAREER_AGENT, "Find internships I'm eligible for and identify my skill gaps"))
    objective = _EVENTS_SKILL_OBJECTIVE if with_career else _EVENTS_DISCOVERY_OBJECTIVE
    if with_schedule:
        objective += _NO_CONFLICT_SUFFIX
    _add(AgentName.EVENTS_OPPORTUNITY_AGENT, objective, deps=upstream)
    return MissionPlan(
        mission_id=mission_id, goal=goal, tasks=tasks, unsupported_requests=list(unsupported_requests or []),
        selection_required_actions=list(selection_required_actions or []),
    )


def _build_single_agent_plan(mission_id: str, goal: str, agent: AgentName) -> MissionPlan:
    task = MissionTask(
        task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=agent,
        objective=goal, dependencies=[], requires_evidence=True,
    )
    return MissionPlan(mission_id=mission_id, goal=goal, tasks=[task])


def _route_by_domain(mission_id: str, goal: str, supported: set) -> Optional[MissionPlan]:
    """Generic keyword routing for goals none of the specific shapes matched.

    Returns None to fall through to the Academic clause-splitting path.
    """
    q = goal.lower()
    events_ok = AgentName.EVENTS_OPPORTUNITY_AGENT in supported
    career_ok = AgentName.CAREER_AGENT in supported
    schedule_ok = AgentName.ACADEMIC_AGENT in supported

    if events_ok and _mentions(q, _EVENTS_WORDS):
        with_schedule = schedule_ok and _mentions(q, _CONFLICT_WORDS)
        if _mentions(q, _REGISTRATION_WORDS):
            # Registration wanted but no event named (a named one is caught by
            # _is_event_registration_goal): discover candidates, never pick one.
            return _build_events_plan(
                mission_id, goal, with_career=False, with_schedule=schedule_ok,
                unsupported_requests=[_UNNAMED_EVENT_NOTE], selection_required_actions=["register_event"],
            )
        with_career = career_ok and "skill" in q
        return _build_events_plan(mission_id, goal, with_career=with_career, with_schedule=with_schedule)
    if career_ok and _mentions(q, _CAREER_WORDS):
        return _build_single_agent_plan(mission_id, goal, AgentName.CAREER_AGENT)
    if AgentName.CAMPUS_SERVICES_AGENT in supported and _mentions(q, _SERVICES_WORDS):
        return _build_single_agent_plan(mission_id, goal, AgentName.CAMPUS_SERVICES_AGENT)
    if len(supported) > 1 and not _mentions(q, _ACADEMIC_WORDS):
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[], unsupported_requests=[_UNSUPPORTED_GOAL_NOTE])
    return None


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
    if "skill" in q and any(k in q for k in ("missing", "lack", "gap")):
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

    def plan_enquiry(self, query: str) -> EnquiryPlan:
        """Deterministic keyword routing for the read-only Enquiry Agent."""
        q = query.lower()
        consults: List[EnquiryConsultation] = []

        def ask(agent: str, objective: str) -> None:
            if not any(c.agent == agent and c.objective == objective for c in consults):
                consults.append(EnquiryConsultation(agent=agent, objective=objective))

        live_class = any(k in q for k in _LIVE_CLASS_PHRASES)
        action_agent = None
        if _mentions(q, _REGISTRATION_WORDS) or "apply for" in q or "apply to" in q:
            action_agent = "placements" if _mentions(q, _CAREER_WORDS) else "events"
        elif any(k in q for k in ("file a complaint", "raise a complaint", "submit a complaint", "lodge a complaint")):
            action_agent = "complaints"

        broad = any(k in q for k in ("today", "important", "anything", "what do i have", "overview", "summary", "this week"))
        if live_class:
            ask("academic", "What is my timetable?")
        if broad or _mentions(q, ("timetable", "schedule", "class")) and not live_class:
            ask("academic", "What is my timetable?")
        if broad or "exam" in q:
            ask("academic", "When are my exams?")
        if "attend" in q and not live_class:
            ask("academic", query)
        if broad or _mentions(q, _EVENTS_WORDS):
            ask("events", _EVENTS_DISCOVERY_OBJECTIVE)
        if broad or _mentions(q, _CAREER_WORDS) or "deadline" in q:
            ask("placements", "Find internships I'm eligible for and identify my skill gaps")
        if broad or _mentions(q, _SERVICES_WORDS):
            ask("complaints", "Check my complaints and tell me whether any are overdue.")
        out_of_scope = None
        if not consults and action_agent is None:
            out_of_scope = "This question is outside what the campus agents can look up."
        return EnquiryPlan(
            consultations=consults[:6], action_requested=action_agent is not None, action_agent=action_agent,
            asks_live_class_status=live_class, out_of_scope=out_of_scope,
        )

    def plan_permission_request(self, message: str) -> PermissionIntent:
        """Deterministic keyword interpretation for the Permission Agent."""
        return _permission_intent(message)

    def plan_faculty_query(self, message: str) -> FacultyQueryPlan:
        """Deterministic keyword classification for the faculty agents."""
        return _faculty_plan(message)

    def plan_hod_query(self, message: str) -> HodQueryPlan:
        """Deterministic keyword classification for the HOD agents."""
        return _hod_plan(message)

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
            return _build_event_registration_plan(
                mission_id, goal, with_schedule=AgentName.ACADEMIC_AGENT in supported
            )
        if _is_calendar_creation_goal(goal) and AgentName.ACTION_AGENT in supported:
            return _build_calendar_creation_plan(mission_id, goal)
        if _is_case_creation_goal(goal) and AgentName.ACTION_AGENT in supported:
            return _build_case_creation_plan(mission_id, goal)
        if _is_career_prep_goal(goal) and _CAREER_PREP_AGENTS.issubset(supported):
            return _build_career_prep_plan(mission_id, goal)
        if _is_campus_services_goal(goal) and AgentName.CAMPUS_SERVICES_AGENT in supported:
            return _build_campus_services_plan(mission_id, goal)
        routed = _route_by_domain(mission_id, goal, supported)
        if routed is not None:
            return routed

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
            if context.skill_gaps:
                return f"No upcoming event matches your skill gaps ({', '.join(context.skill_gaps)})."
            return "I couldn't find any relevant upcoming events."

        # "No conflicts found" is only a claim when a check actually ran
        # against the student's timetable/exams; otherwise say it wasn't checked.
        unchecked = [a for a in context.assessments if not a.conflict_check_performed]
        conflict_free = [
            a for a in context.assessments
            if a.conflict_check_performed and not a.timetable_conflicts and not a.exam_conflicts
        ]
        conflicted = [a for a in context.assessments if a.timetable_conflicts or a.exam_conflicts]
        lines: List[str] = []

        def _line(a) -> str:
            if a.already_registered:
                note = f" (you're already registered -- {a.registration_status})"
            else:
                note = f" ({a.availability.value})"
            if a.matched_skill_gaps:
                note += f" -- matches skill gap: {', '.join(a.matched_skill_gaps)}"
            return f"- {a.event.title} on {a.event.start_at.isoformat()} at {a.event.location}{note}"

        if conflict_free:
            lines.append("Events with no schedule conflicts:")
            lines.extend(_line(a) for a in conflict_free)

        if unchecked:
            lines.append("Matching events (schedule conflicts NOT checked in this step: no timetable/exam data was provided to it):")
            lines.extend(_line(a) for a in unchecked)

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
