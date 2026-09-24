"""Phase 12B -- live runtime correctness, reproduced offline.

A real Groq run (openai/gpt-oss-120b) exposed three problems. Each is
replayed here with a scripted provider that returns the *same shape* of
output the live model produced (plans go through the real
``_proposal_to_plan`` conversion), so no test needs a network or a key:

1. "How many more classes do I need..." was classified attendance_status.
   The intent definitions now travel in the structured-output schema.
2. "Find a useful workshop for my missing technical skills" failed because
   the upstream Career task ("Identify my missing technical skills ...") was
   classified ``unknown``. Separately, planner filler words ("campus",
   "student") made unrelated events look relevant.
3. "Find a suitable event, check my schedule and prepare my registration"
   produced a register_event task with no event named, and a replan reused
   task ids for different tasks, so a stale events answer was presented as
   the result. No action may be proposed for a target the student never named.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import pytest
from sqlalchemy import select

from app.agents.action.agent import ActionAgent
from app.agents.career.agent import CareerAgent
from app.agents.events.agent import EventsAgent
from app.api.main import _build_registry
from app.db.models.events import EventRegistration
from app.db.models.identity import Student
from app.db.models.mission import AgentRun, ApprovalRecord, ToolCallRecord
from app.db.repositories.missions import get_latest_approval_for_step
from app.graph.orchestrator import MissionOrchestrator, redefined_task_ids, unconfirmed_action_targets
from app.graph.registry import AgentRegistry
from app.llm.providers.anthropic_provider import AnthropicLLMProvider, _PlanProposal, _proposal_to_plan
from app.llm.providers.mock import MockLLMProvider
from app.rules.action_preconditions import resolve_target_provenance, target_named_in_goal
from app.schemas.academic import AcademicIntent, CourseSummary
from app.schemas.action import TargetSource
from app.graph.dispatcher import build_task_input_facts
from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.career import CareerIntent, CareerIntentResult
from app.schemas.enums import (
    AgentName,
    AgentResultStatus,
    ApprovalStatus,
    MissionStatus,
    TaskStatus,
    UserRole,
    VerificationStatus,
)
from app.schemas.mission import MissionPlan, MissionTask
from app.services.approval_gate import ApprovalGate
from app.services.context import ContextService
from app.tools.build import build_default_tool_registry
from tests.graph_doubles import AlwaysFailAgent, FixedPlanLLMProvider, SuccessAgent, _outcome

DEMO_STUDENT = "STU-DEMO-001"
CLEAN_EVENT_ID = 10  # Competitive Coding Contest -- conflict-free for STU-DEMO-001 at seed time
COURSES = [
    CourseSummary(course_code="CS301", title="Operating Systems", credits=4, semester=5, instructor="Dr. A"),
    CourseSummary(course_code="CS303", title="Computer Networks", credits=4, semester=5, instructor="Dr. C"),
]


# ---------------------------------------------------------------------------
# Scripted "live" planner: real Mock classification/rendering, but plans are
# the proposals a live model returned, converted by the live providers' own
# _proposal_to_plan.
# ---------------------------------------------------------------------------


class ScriptedLivePlanner(MockLLMProvider):
    name = "scripted-live"

    def __init__(self, *proposals: Dict[str, Any]) -> None:
        self._proposals = list(proposals)
        self.plan_calls = 0

    def plan_mission(self, mission_id: str, goal: str, *, supported_agents: List[AgentName]) -> MissionPlan:
        proposal = self._proposals[min(self.plan_calls, len(self._proposals) - 1)]
        self.plan_calls += 1
        return _proposal_to_plan(mission_id, goal, _PlanProposal.model_validate(proposal))


def _task(index: int, agent: str, objective: str, deps: Optional[List[int]] = None, action: Optional[dict] = None) -> dict:
    task = {"index": index, "objective": objective, "agent": agent, "depends_on_indices": deps or [], "requires_evidence": True}
    if action is not None:
        task["action"] = action
    return task


def _orchestrator(session_factory, knowledge_service, llm) -> MissionOrchestrator:
    registry = _build_registry(knowledge_service, llm, build_default_tool_registry())
    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm)


def _run(orchestrator: MissionOrchestrator, goal: str):
    return orchestrator.run_mission(goal, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)


def _rows(session_factory, model, mission_id: str) -> list:
    with session_factory() as session:
        return list(session.execute(select(model).where(model.mission_id == mission_id)).scalars().all())


def _audit_types(session_factory, mission_id: str) -> List[str]:
    with session_factory() as session:
        return [event.event_type for event in ContextService(session).list_audit_events(mission_id)]


# ---------------------------------------------------------------------------
# 1. Attendance status vs recovery
# ---------------------------------------------------------------------------

RECOVERY_PARAPHRASES = [
    "How many more classes do I need to attend in Computer Networks to reach the required attendance?",
    "How can I reach 75% attendance in OS?",
    "What must I attend to recover my Operating Systems attendance?",
    "What do I need to do to bring my Computer Networks attendance up to the requirement?",
    "Can I still get my CS303 attendance back to 75%, and how many classes would that take?",
]
STATUS_PARAPHRASES = [
    "What is my attendance in Computer Networks?",
    "How much attendance do I currently have in OS?",
    "Show my Operating Systems attendance percentage.",
    "Is my CS303 attendance above the requirement right now?",
    "What's my current attendance record for Computer Networks?",
]


@pytest.mark.parametrize("query", RECOVERY_PARAPHRASES)
def test_recovery_paraphrases_classify_as_recovery(query: str) -> None:
    assert MockLLMProvider().classify_academic_intent(query, COURSES).intent == AcademicIntent.ATTENDANCE_RECOVERY


@pytest.mark.parametrize("query", STATUS_PARAPHRASES)
def test_status_paraphrases_classify_as_status(query: str) -> None:
    assert MockLLMProvider().classify_academic_intent(query, COURSES).intent == AcademicIntent.ATTENDANCE_STATUS


class _RecordingAnthropicClient:
    """Records the tool schema a real provider sends; replies with a fixed tool call."""

    def __init__(self, tool_input: Dict[str, Any]) -> None:
        self.tool_input = tool_input
        self.kwargs: List[Dict[str, Any]] = []

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        name = kwargs["tools"][0]["name"]

        class _Block:
            type = "tool_use"

        block = _Block()
        block.name, block.input = name, self.tool_input
        return type("Resp", (), {"content": [block], "stop_reason": "tool_use"})()


def test_live_tool_schema_carries_the_status_vs_recovery_definitions() -> None:
    client = _RecordingAnthropicClient({"intent": "attendance_recovery", "raw_course_reference": "Computer Networks"})
    provider = AnthropicLLMProvider(model="claude-sonnet-5", client=client)
    provider.classify_academic_intent(RECOVERY_PARAPHRASES[0], COURSES)

    description = client.kwargs[0]["tools"][0]["input_schema"]["properties"]["intent"]["description"]
    assert "attendance_status" in description and "attendance_recovery" in description
    assert "How many (more) classes do I need to attend?" in description
    assert "What is my attendance?" in description
    assert "attendance_recovery" in client.kwargs[0]["system"]


def test_groq_request_carries_the_same_intent_definitions() -> None:
    httpx = pytest.importorskip("httpx")
    from app.llm.providers.groq import GroqLLMProvider

    bodies: List[Dict[str, Any]] = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        name = body["tool_choice"]["function"]["name"]
        args = {"intent": "attendance_recovery"} if name == "classify_academic_intent" else {"intent": "opportunity_discovery"}
        message = {"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": message}]})

    client = httpx.Client(base_url="https://groq.test/openai/v1", transport=httpx.MockTransport(respond))
    provider = GroqLLMProvider(api_key="k", client=client, sleep=lambda _: None)
    provider.classify_academic_intent(RECOVERY_PARAPHRASES[0], COURSES)
    provider.classify_career_intent("Identify my missing technical skills based on my current profile and coursework.")

    academic_schema = bodies[0]["tools"][0]["function"]["parameters"]["properties"]["intent"]
    career_schema = bodies[1]["tools"][0]["function"]["parameters"]["properties"]["intent"]
    assert "attendance_recovery" in academic_schema["description"]
    assert "skill gaps" in career_schema["description"] and "opportunity_discovery" in career_schema["description"]


# ---------------------------------------------------------------------------
# 2. Career skill gaps -> Events
# ---------------------------------------------------------------------------

LIVE_SKILL_GAP_OBJECTIVE = "Identify my missing technical skills based on my current profile and coursework."
LIVE_EVENTS_OBJECTIVE = "Find campus workshops or competitions that address the missing technical skills identified in task 1."
EVENTS_GOAL = "Find a useful workshop for my missing technical skills."
LIVE_EVENTS_PROPOSAL = {
    "tasks": [
        _task(1, "career_agent", LIVE_SKILL_GAP_OBJECTIVE),
        _task(2, "events_opportunity_agent", LIVE_EVENTS_OBJECTIVE, deps=[1]),
    ],
    "unsupported_requests": [],
}


def _events_message(query: str, facts: Dict[str, Any]) -> AgentMessage:
    return AgentMessage(
        message_id="msg-1", mission_id="mission-1", task_id="task-1", source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.EVENTS_OPPORTUNITY_AGENT, objective=query, facts={"student_id": DEMO_STUDENT, "query": query, **facts},
    )


def _events_agent(seeded_session, knowledge_service) -> EventsAgent:
    return EventsAgent(session=seeded_session, knowledge_service=knowledge_service, llm_provider=MockLLMProvider())


@pytest.mark.parametrize(
    "objective",
    [LIVE_SKILL_GAP_OBJECTIVE, "What skills am I missing?", "Which technical skills do I lack for internships?"],
)
def test_skill_gap_requests_are_opportunity_discovery(objective: str) -> None:
    assert MockLLMProvider().classify_career_intent(objective).intent == CareerIntent.OPPORTUNITY_DISCOVERY


def test_career_publishes_skill_gaps_for_the_live_objective(seeded_session, knowledge_service) -> None:
    agent = CareerAgent(session=seeded_session, knowledge_service=knowledge_service, llm_provider=MockLLMProvider())
    outcome = agent.handle(
        AgentMessage(
            message_id="m", mission_id="mission-1", task_id="task-1", source=AgentName.MISSION_ORCHESTRATOR,
            target=AgentName.CAREER_AGENT, objective=LIVE_SKILL_GAP_OBJECTIVE,
            facts={"student_id": DEMO_STUDENT, "query": LIVE_SKILL_GAP_OBJECTIVE},
        )
    )
    assert outcome.verification.status == VerificationStatus.VERIFIED
    assert isinstance(outcome.agent_result.facts["skill_gaps"], list) and outcome.agent_result.facts["skill_gaps"]


def test_valid_skill_gap_match_is_verified_and_filler_words_match_nothing(seeded_session, knowledge_service) -> None:
    outcome = _events_agent(seeded_session, knowledge_service).handle(
        _events_message(LIVE_EVENTS_OBJECTIVE, {"skill_gaps": ["Cloud Computing (AWS)", "Docker"]})
    )

    assert outcome.verification.status == VerificationStatus.VERIFIED
    assessments = outcome.agent_result.facts["assessments"]
    # Only the event that shares a term with a gap -- not "Campus Career Fair"
    # via the filler word "campus".
    assert [a["event"]["title"] for a in assessments] == ["Tech Talk: Cloud Native Systems"]
    assert assessments[0]["matched_skill_gaps"] == ["Cloud Computing (AWS)"]
    assert outcome.agent_result.facts["matched_against_skill_gaps"] == ["Cloud Computing (AWS)", "Docker"]


def test_no_matching_event_is_a_grounded_empty_result(seeded_session, knowledge_service) -> None:
    outcome = _events_agent(seeded_session, knowledge_service).handle(
        _events_message(LIVE_EVENTS_OBJECTIVE, {"skill_gaps": ["Kubernetes", "Rust"]})
    )

    assert outcome.verification.status != VerificationStatus.FAILED
    assert "assessments" not in outcome.agent_result.facts  # nothing fabricated
    assert "No upcoming event matches the skill gaps: Kubernetes, Rust." in outcome.verification.issues
    assert "No upcoming event matches your skill gaps (Kubernetes, Rust)" in outcome.response_text


def test_empty_upstream_skill_gaps_never_fall_back_to_every_event(seeded_session, knowledge_service) -> None:
    outcome = _events_agent(seeded_session, knowledge_service).handle(
        _events_message("Find workshops related to my skill gaps", {"skill_gaps": []})
    )
    assert "assessments" not in outcome.agent_result.facts
    assert outcome.verification.status != VerificationStatus.FAILED
    assert any("No skill gaps were identified" in issue for issue in outcome.verification.issues)


@pytest.mark.parametrize("malformed", ["Docker, Kubernetes", [None], [{"skill": "Docker"}], ["  "], 42])
def test_malformed_upstream_skill_gaps_fail_verification(seeded_session, knowledge_service, malformed) -> None:
    outcome = _events_agent(seeded_session, knowledge_service).handle(_events_message(LIVE_EVENTS_OBJECTIVE, {"skill_gaps": malformed}))

    assert outcome.verification.status == VerificationStatus.FAILED
    assert any(check.name == "skill_gaps_well_formed" and not check.passed for check in outcome.verification.checks)
    assert "assessments" not in outcome.agent_result.facts


def test_generic_planner_objective_does_not_narrow_to_an_arbitrary_event(seeded_session, knowledge_service) -> None:
    """The live run surfaced only "Blood Donation Camp" (organizer: *Student*
    Welfare Office) because "student" counted as a match term."""
    outcome = _events_agent(seeded_session, knowledge_service).handle(
        _events_message("Find campus events, workshops, or competitions that could be suitable for the student.", {})
    )
    titles = {a["event"]["title"] for a in outcome.agent_result.facts["assessments"]}
    assert {"Blood Donation Camp", "Competitive Coding Contest", "Tech Talk: Cloud Native Systems"} <= titles
    assert outcome.verification.status == VerificationStatus.VERIFIED


def test_live_events_mission_matches_workshops_to_career_skill_gaps(session_factory, seeded_session, knowledge_service) -> None:
    llm = ScriptedLivePlanner(LIVE_EVENTS_PROPOSAL)
    final = _run(_orchestrator(session_factory, knowledge_service, llm), EVENTS_GOAL)

    assert final["mission_status"] == MissionStatus.COMPLETED
    career_id, events_id = (t.task_id for t in final["plan"].tasks)
    gaps = final["agent_results"][career_id].facts["skill_gaps"]
    events_facts = final["agent_results"][events_id].facts
    assert events_facts["matched_against_skill_gaps"] == gaps  # propagated intact
    assert events_facts["assessments"]
    assert all(a["matched_skill_gaps"] and set(a["matched_skill_gaps"]) <= set(gaps) for a in events_facts["assessments"])
    assert final["verifications"][events_id].status == VerificationStatus.VERIFIED


class _UnknownCareerPlanner(ScriptedLivePlanner):
    def classify_career_intent(self, query: str) -> CareerIntentResult:
        return CareerIntentResult(intent=CareerIntent.UNKNOWN)


def test_unknown_career_intent_still_fails_verification(session_factory, seeded_session, knowledge_service) -> None:
    """The live failure itself: the verifier is not relaxed -- an unclassifiable
    career request fails, and the Events task never runs on missing gaps."""
    final = _run(_orchestrator(session_factory, knowledge_service, _UnknownCareerPlanner(LIVE_EVENTS_PROPOSAL)), EVENTS_GOAL)

    assert final["mission_status"] == MissionStatus.FAILED
    career_id, events_id = (t.task_id for t in final["plan"].tasks)
    assert "Unsupported career request." in final["verifications"][career_id].issues
    assert events_id not in final["agent_results"]


# ---------------------------------------------------------------------------
# 3. Action target provenance (pure rule)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal, title, expected",
    [
        ("Register me for Competitive Coding Contest if it doesn't clash.", "Competitive Coding Contest", TargetSource.USER_GOAL),
        ("register me for the 'competitive coding contest'", "Competitive Coding Contest", TargetSource.USER_GOAL),
        ("Sign me up for Tech Talk: Cloud Native Systems", "Tech Talk: Cloud Native Systems", TargetSource.USER_GOAL),
        ("Register me for the coding contest", "Competitive Coding Contest", TargetSource.UNCONFIRMED),  # partial name
        ("Find a suitable event and prepare my registration.", "Blood Donation Camp", TargetSource.UNCONFIRMED),  # agent's pick
        ("Find a suitable event and prepare my registration.", "", TargetSource.UNCONFIRMED),
        ("Find a suitable event and prepare my registration.", None, TargetSource.UNCONFIRMED),
    ],
)
def test_register_event_target_must_be_named_by_the_student(goal: str, title: Optional[str], expected: TargetSource) -> None:
    constraints = {"tool_name": "register_event"}
    if title is not None:
        constraints["event_title"] = title
    assert resolve_target_provenance("register_event", constraints, goal).source == expected


def test_target_match_is_whole_word_only() -> None:
    assert not target_named_in_goal("AI", "I said register me")
    assert target_named_in_goal("AI", "Register me for the AI event")
    assert not target_named_in_goal("   ", "anything")


def test_provenance_for_other_tools_and_direct_callers() -> None:
    # An event id cannot come from the student's words.
    assert resolve_target_provenance("register_event", {"event_id": 10}, "Register me for it").source == TargetSource.UNCONFIRMED
    # No goal: the caller (an explicit approval edit) supplied the target.
    assert resolve_target_provenance("register_event", {"event_id": 10}, None).source == TargetSource.CALLER_SUPPLIED
    assert resolve_target_provenance("register_event", {}, None).source == TargetSource.UNCONFIRMED
    assert resolve_target_provenance("create_calendar_event", {"title": "Study"}, "Add Study to my calendar").source == TargetSource.NOT_REQUIRED
    unnamed_source = resolve_target_provenance(
        "create_calendar_event", {"title": "Prep", "source_event_title": "Music Night"}, "Add a prep session to my calendar"
    )
    assert unnamed_source.source == TargetSource.UNCONFIRMED
    assert resolve_target_provenance("create_campus_case", {"category": "hostel"}, "File a hostel complaint").source == TargetSource.NOT_REQUIRED


# ---------------------------------------------------------------------------
# 4. Unspecified action target -- end to end
# ---------------------------------------------------------------------------

UNNAMED_GOAL = "Find a suitable event and prepare my registration."
NAMED_GOAL = "Register me for Competitive Coding Contest if it doesn't clash."
CANDIDATE_OBJECTIVE = "Find campus events, workshops, or competitions that could be suitable for the student."
# The live replan shape: an action task with no event named.
UNNAMED_ACTION_PROPOSAL = {
    "tasks": [
        _task(1, "academic_agent", "Provide the student's weekly class timetable."),
        _task(2, "academic_agent", "Provide the student's exam schedule."),
        _task(3, "events_opportunity_agent", CANDIDATE_OBJECTIVE, deps=[1, 2]),
        _task(4, "action_agent", "Prepare a registration for the chosen event.", deps=[1, 2, 3], action={"tool_name": "register_event"}),
    ],
    "unsupported_requests": [],
}


def _assert_nothing_proposed(session_factory, mission_id: str) -> None:
    assert _rows(session_factory, ApprovalRecord, mission_id) == []
    assert _rows(session_factory, ToolCallRecord, mission_id) == []
    assert [r for r in _rows(session_factory, AgentRun, mission_id) if r.agent == AgentName.ACTION_AGENT] == []


def test_unnamed_registration_returns_candidates_and_requires_selection(session_factory, seeded_session, knowledge_service) -> None:
    final = _run(_orchestrator(session_factory, knowledge_service, ScriptedLivePlanner(UNNAMED_ACTION_PROPOSAL)), UNNAMED_GOAL)
    mission_id = final["mission_id"]
    action_id = final["plan"].tasks[3].task_id
    events_id = final["plan"].tasks[2].task_id

    assert final["mission_status"] == MissionStatus.COMPLETED  # a safe clarification, not a failure
    assert final["task_status"][action_id] == TaskStatus.SKIPPED
    _assert_nothing_proposed(session_factory, mission_id)
    assert "action_target_unconfirmed" in _audit_types(session_factory, mission_id)

    # Candidates are discovered and conflict-checked against timetable/exams...
    assessments = final["agent_results"][events_id].facts["assessments"]
    assert len(assessments) > 1 and all(a["conflict_check_performed"] for a in assessments)
    # ...and presented as candidates, never as a chosen target.
    text = final["final_result"]
    assert "Candidate events (recommendations only -- none has been selected for you" in text
    assert "User selection required: register_event was not prepared" in text
    assert "awaiting human approval" not in text


def test_planner_picked_target_is_not_treated_as_the_students_choice(session_factory, seeded_session, knowledge_service) -> None:
    picked = json.loads(json.dumps(UNNAMED_ACTION_PROPOSAL))
    picked["tasks"][3]["action"]["event_title"] = "Blood Donation Camp"  # e.g. "first/best result"
    final = _run(_orchestrator(session_factory, knowledge_service, ScriptedLivePlanner(picked)), UNNAMED_GOAL)

    assert final["task_status"][final["plan"].tasks[3].task_id] == TaskStatus.SKIPPED
    _assert_nothing_proposed(session_factory, final["mission_id"])
    assert "'Blood Donation Camp' was not named in the request." in final["final_result"]


def test_plan_without_action_task_labels_events_as_candidates(session_factory, seeded_session, knowledge_service) -> None:
    """The live planner's own safe answer: discovery only, plus a note."""
    proposal = {
        "tasks": UNNAMED_ACTION_PROPOSAL["tasks"][:3],
        "unsupported_requests": ["Preparing a registration requires the exact event title, which the student has not provided."],
    }
    final = _run(_orchestrator(session_factory, knowledge_service, ScriptedLivePlanner(proposal)), UNNAMED_GOAL)

    assert final["mission_status"] == MissionStatus.COMPLETED
    _assert_nothing_proposed(session_factory, final["mission_id"])
    assert "Candidate events (recommendations only" in final["final_result"]
    assert "Not handled by this mission: Preparing a registration requires the exact event title" in final["final_result"]


NAMED_ACTION_PROPOSAL = {
    "tasks": [
        _task(1, "events_opportunity_agent", "Find the Competitive Coding Contest event."),
        _task(2, "academic_agent", "What is my timetable?"),
        _task(3, "academic_agent", "When are my exams?"),
        _task(
            4, "action_agent", "Register for the Competitive Coding Contest.", deps=[1, 2, 3],
            action={"tool_name": "register_event", "event_title": "Competitive Coding Contest"},
        ),
    ],
    "unsupported_requests": [],
}


def test_explicitly_named_target_keeps_the_full_approval_flow(session_factory, seeded_session, knowledge_service) -> None:
    llm = ScriptedLivePlanner(NAMED_ACTION_PROPOSAL)
    orchestrator = _orchestrator(session_factory, knowledge_service, llm)
    final = _run(orchestrator, NAMED_GOAL)
    plan = final["plan"]
    action = plan.tasks[3]

    upstream_agents = sorted(t.agent.value for t in plan.tasks if t.task_id in action.dependencies)
    assert upstream_agents == ["academic_agent", "academic_agent", "events_opportunity_agent"]
    assert final["mission_status"] == MissionStatus.NEEDS_APPROVAL
    facts = final["agent_results"][action.task_id].facts
    assert facts["target_provenance"]["source"] == "user_goal"
    assert facts["target_provenance"]["target_value"] == "Competitive Coding Contest"
    supporting = facts["proposal"]["supporting_facts"]
    assert supporting["target_provenance"]["source"] == "user_goal"
    assert supporting["schedule_check"]["performed"] is True
    assert "Candidate events" not in final["final_result"]

    with session_factory() as session:
        approval = get_latest_approval_for_step(session, action.task_id)
        assert approval.status == ApprovalStatus.PENDING
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    resumed = orchestrator.resume_mission(final["mission_id"])
    assert resumed["mission_status"] == MissionStatus.COMPLETED
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
        registrations = session.execute(
            select(EventRegistration).where(EventRegistration.event_id == CLEAN_EVENT_ID, EventRegistration.student_id == student.id)
        ).scalars().all()
    assert len(registrations) == 1
    assert llm.plan_calls == 1  # resuming never re-plans


def _action_message(goal: Optional[str], constraints: Dict[str, Any], task_id: str = "t-1") -> AgentMessage:
    facts: Dict[str, Any] = {"student_id": DEMO_STUDENT}
    if goal is not None:
        facts["mission_goal"] = goal
    return AgentMessage(
        message_id="msg-1", mission_id="m-12b", task_id=task_id, source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACTION_AGENT, objective="register", facts=facts, constraints=constraints,
    )


def test_action_agent_independently_refuses_an_unnamed_target(seeded_session, knowledge_service) -> None:
    """Second layer: even if a task slipped past the Orchestrator gate."""
    context = ContextService(seeded_session)
    context.create_mission("m-12b", DEMO_STUDENT, UserRole.STUDENT, UNNAMED_GOAL)
    context.create_mission_step("t-1", "m-12b", AgentName.ACTION_AGENT, "register")
    agent = ActionAgent(session=seeded_session, knowledge_service=knowledge_service, tool_gateway=build_default_tool_registry())

    outcome = agent.handle(_action_message(UNNAMED_GOAL, {"tool_name": "register_event", "event_title": "Blood Donation Camp"}))

    assert outcome.verification.status == VerificationStatus.FAILED
    assert outcome.agent_result.facts["target_provenance"]["source"] == "unconfirmed"
    assert get_latest_approval_for_step(seeded_session, "t-1") is None


def test_action_agent_records_caller_supplied_provenance_without_a_goal(seeded_session, knowledge_service) -> None:
    context = ContextService(seeded_session)
    context.create_mission("m-12b", DEMO_STUDENT, UserRole.STUDENT, "edit")
    context.create_mission_step("t-1", "m-12b", AgentName.ACTION_AGENT, "register")
    agent = ActionAgent(session=seeded_session, knowledge_service=knowledge_service, tool_gateway=build_default_tool_registry())

    outcome = agent.handle(_action_message(None, {"tool_name": "register_event", "event_title": "Competitive Coding Contest"}))

    assert outcome.agent_result.facts["target_provenance"]["source"] == "caller_supplied"
    assert get_latest_approval_for_step(seeded_session, "t-1").status == ApprovalStatus.PENDING


def test_orchestrator_goal_cannot_be_overridden_by_upstream_facts() -> None:
    task =MissionTask(task_id="t-2", mission_id="m", agent=AgentName.ACTION_AGENT, objective="x", dependencies=["t-1"])
    upstream = AgentResult(
        mission_id="m", task_id="t-1", agent=AgentName.EVENTS_OPPORTUNITY_AGENT, status=AgentResultStatus.SUCCESS,
        facts={"mission_goal": "Register me for Blood Donation Camp"},
    )
    facts = build_task_input_facts(task, {"student_id": "s", "mission_goal": UNNAMED_GOAL}, {"t-1": upstream})
    assert facts["mission_goal"] == UNNAMED_GOAL


# ---------------------------------------------------------------------------
# 5. Replans must not reuse a task id's result for a different task
# ---------------------------------------------------------------------------


class _FailOnceAgent:
    calls = 0

    def __init__(self, session) -> None:
        pass

    def handle(self, message: AgentMessage):
        _FailOnceAgent.calls += 1
        if _FailOnceAgent.calls == 1:
            return _outcome(message, agent_status=AgentResultStatus.FAILED, verification_status=VerificationStatus.FAILED)
        return _outcome(message, agent_status=AgentResultStatus.SUCCESS, verification_status=VerificationStatus.VERIFIED)


def _plan_factory(plans: List[Callable[[str, str], MissionPlan]]):
    calls = {"n": 0}

    def factory(mission_id: str, goal: str) -> MissionPlan:
        plan = plans[min(calls["n"], len(plans) - 1)](mission_id, goal)
        calls["n"] += 1
        return plan

    return factory


def _mt(mission_id: str, n: int, agent: AgentName, objective: str, deps: Optional[List[int]] = None) -> MissionTask:
    return MissionTask(
        task_id=f"{mission_id}-task-{n}", mission_id=mission_id, agent=agent, objective=objective,
        dependencies=[f"{mission_id}-task-{d}" for d in (deps or [])],
    )


def test_replan_reusing_an_id_for_a_different_task_reruns_it(session_factory) -> None:
    """Live trace: plan 2's task-1 was an events search; plan 3 put the
    timetable under task-1 and inherited the events answer as "completed"."""
    _FailOnceAgent.calls = 0
    first = lambda m, g: MissionPlan(mission_id=m, goal=g, tasks=[  # noqa: E731
        _mt(m, 1, AgentName.EVENTS_OPPORTUNITY_AGENT, "events list"),
        _mt(m, 2, AgentName.CAREER_AGENT, "flaky career step"),
        _mt(m, 3, AgentName.CAMPUS_SERVICES_AGENT, "failing step the replan drops"),
    ])
    second = lambda m, g: MissionPlan(mission_id=m, goal=g, tasks=[  # noqa: E731
        _mt(m, 1, AgentName.ACADEMIC_AGENT, "timetable"),
        _mt(m, 2, AgentName.CAREER_AGENT, "flaky career step"),
    ])
    registry = AgentRegistry()
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, SuccessAgent)
    registry.register(AgentName.ACADEMIC_AGENT, SuccessAgent)
    registry.register(AgentName.CAREER_AGENT, _FailOnceAgent)
    registry.register(AgentName.CAMPUS_SERVICES_AGENT, AlwaysFailAgent)
    orchestrator = MissionOrchestrator(
        session_factory=session_factory, registry=registry,
        llm_provider=FixedPlanLLMProvider(plan_factory=_plan_factory([first, second])),
    )

    final = orchestrator.run_mission("replan goal", user_id=DEMO_STUDENT, user_role=UserRole.STUDENT)
    mission_id = final["mission_id"]
    task_1, task_3 = f"{mission_id}-task-1", f"{mission_id}-task-3"

    assert final["mission_status"] == MissionStatus.COMPLETED
    assert final["responses"][task_1] == "[verified] timetable"  # re-run as the new task
    assert final["agent_results"][task_1].agent == AgentName.ACADEMIC_AGENT
    assert "events list" not in final["final_result"]
    assert final["task_status"][task_3] == TaskStatus.SKIPPED  # superseded, not left pending
    audit = _audit_types(session_factory, mission_id)
    assert "task_redefined" in audit and "task_superseded" in audit
    with session_factory() as session:
        step = next(s for s in ContextService(session).get_mission(mission_id).steps if s.step_id == task_1)
        assert (step.agent, step.objective) == (AgentName.ACADEMIC_AGENT, "timetable")


def test_redefined_ids_cascade_to_dependents_but_identical_tasks_are_kept() -> None:
    old = MissionPlan(mission_id="m", goal="g", tasks=[
        _mt("m", 1, AgentName.ACADEMIC_AGENT, "timetable"),
        _mt("m", 2, AgentName.CAREER_AGENT, "gaps"),
        _mt("m", 3, AgentName.EVENTS_OPPORTUNITY_AGENT, "workshops", deps=[2]),
    ])
    new = MissionPlan(mission_id="m", goal="g", tasks=[
        _mt("m", 1, AgentName.ACADEMIC_AGENT, "timetable"),
        _mt("m", 2, AgentName.CAREER_AGENT, "different gaps question"),
        _mt("m", 3, AgentName.EVENTS_OPPORTUNITY_AGENT, "workshops", deps=[2]),
    ])
    assert redefined_task_ids(old, new) == ["m-task-2", "m-task-3"]
    assert redefined_task_ids(old, old) == []


def test_unconfirmed_targets_helper_ignores_read_only_tasks() -> None:
    plan = _proposal_to_plan("m", UNNAMED_GOAL, _PlanProposal.model_validate(UNNAMED_ACTION_PROPOSAL))
    assert list(unconfirmed_action_targets(plan, UNNAMED_GOAL)) == ["m-task-4"]
    named = _proposal_to_plan("m", NAMED_GOAL, _PlanProposal.model_validate(NAMED_ACTION_PROPOSAL))
    assert unconfirmed_action_targets(named, NAMED_GOAL) == {}
