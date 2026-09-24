"""Phase 9: MockLLMProvider's generic natural-language routing.

The mock is the offline stand-in for the real planner, so these tests pin
routing by *domain vocabulary* across several phrasings per scenario -- never
one hardcoded sentence -- plus the explicit decline for out-of-scope goals.
"""
from __future__ import annotations

import pytest

from app.llm.providers.mock import MockLLMProvider, _classify
from app.schemas.academic import AcademicIntent
from app.schemas.enums import AgentName

A, C, E, S, X = (
    AgentName.ACADEMIC_AGENT,
    AgentName.CAREER_AGENT,
    AgentName.EVENTS_OPPORTUNITY_AGENT,
    AgentName.CAMPUS_SERVICES_AGENT,
    AgentName.ACTION_AGENT,
)
ALL = [A, C, E, S, X]
provider = MockLLMProvider()


def _shape(goal: str, supported=ALL):
    plan = provider.plan_mission("m", goal, supported_agents=supported)
    index = {t.task_id: i for i, t in enumerate(plan.tasks, start=1)}
    return plan, [(t.agent, sorted(index[d] for d in t.dependencies)) for t in plan.tasks]


@pytest.mark.parametrize(
    "query",
    [
        "how can I recover? in OS",
        "How do I catch up on attendance in Operating Systems?",
        "how many more classes do I need in OS?",
        "How many classes do I need to attend in OS to reach the required attendance?",
    ],
)
def test_attendance_recovery_phrasings(query: str) -> None:
    assert _classify(query) == AcademicIntent.ATTENDANCE_RECOVERY


def test_recovery_phrasing_does_not_swallow_timetable_questions() -> None:
    assert _classify("How many classes do I have on Monday? Show my timetable") == AcademicIntent.TIMETABLE


@pytest.mark.parametrize(
    "goal",
    [
        "Help me prepare for AI internships without missing important classes.",
        "I want to prepare for internships in machine learning.",
        "Find internships, identify my skill gaps and find workshops that fit my timetable.",
    ],
)
def test_internship_preparation_is_the_cross_agent_mission(goal: str) -> None:
    _, shape = _shape(goal)
    assert shape == [(A, []), (A, []), (C, []), (E, [1, 2, 3])]


@pytest.mark.parametrize(
    "goal",
    ["Find a useful workshop for my missing technical skills.", "Which events would help me close my skill gaps?"],
)
def test_skill_driven_event_discovery_feeds_career_into_events(goal: str) -> None:
    _, shape = _shape(goal)
    assert shape == [(C, []), (E, [1])]


@pytest.mark.parametrize(
    "goal",
    ["Are there any workshops this month that don't clash with my classes?", "Find hackathons that fit my timetable."],
)
def test_clash_aware_event_discovery_depends_on_timetable_and_exams(goal: str) -> None:
    plan, shape = _shape(goal)
    assert shape == [(A, []), (A, []), (E, [1, 2])]
    assert "conflict" in plan.tasks[-1].objective


def test_plain_event_discovery_is_a_single_events_task() -> None:
    _, shape = _shape("What workshops are coming up?")
    assert shape == [(E, [])]


@pytest.mark.parametrize(
    "goal",
    [
        "Find a suitable event, check my schedule and prepare my registration.",
        "Sign me up for a good workshop next week.",
    ],
)
def test_registration_without_a_named_event_never_picks_one(goal: str) -> None:
    plan, shape = _shape(goal)
    assert X not in [agent for agent, _ in shape]  # no action is proposed on a guessed event
    assert shape == [(A, []), (A, []), (E, [1, 2])]
    assert any("no specific event was named" in note for note in plan.unsupported_requests)


def test_registration_with_a_named_event_still_proposes_the_action() -> None:
    plan, shape = _shape("Register me for the 'Competitive Coding Contest' workshop if it doesn't clash with my classes.")
    assert shape == [(E, []), (X, [1])]
    assert plan.tasks[1].constraints == {"tool_name": "register_event", "event_title": "Competitive Coding Contest"}


@pytest.mark.parametrize(
    "goal",
    ["Check my complaints and tell me whether any are overdue.", "Has my hostel complaint breached its SLA?"],
)
def test_complaint_status_goes_to_campus_services(goal: str) -> None:
    _, shape = _shape(goal)
    assert shape == [(S, [])]


def test_career_only_goal_is_a_single_career_task() -> None:
    _, shape = _shape("What's the status of my job applications?")
    assert shape == [(C, [])]


@pytest.mark.parametrize("goal", ["Book me a flight to Goa for the holidays.", "Order a pizza to my room."])
def test_out_of_scope_goals_are_declined_with_an_explanation(goal: str) -> None:
    plan, shape = _shape(goal)
    assert shape == []
    assert plan.unsupported_requests and "doesn't match anything CampusNexus can do" in plan.unsupported_requests[0]


def test_academic_only_registry_keeps_the_original_fallback() -> None:
    _, shape = _shape("Check my OS attendance.", supported=[A])
    assert shape == [(A, [])]


def _assessment(timetable):
    from datetime import datetime, timedelta, timezone

    from app.rules.event_availability import assess_event
    from app.schemas.events import EventSummary

    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    event = EventSummary(
        event_id=1, title="Competitive Coding Contest", description="Contest", category="competition",
        organizer="Coding Club", location="Lab 1", start_at=now + timedelta(days=5),
        end_at=now + timedelta(days=5, hours=2), capacity=50, status="scheduled",
    )
    return assess_event(event, now=now, confirmed_registrations=0, timetable=timetable)


def _render(assessment) -> str:
    from app.schemas.enums import VerificationStatus
    from app.schemas.events import EventsIntent, EventsResponseContext

    context = EventsResponseContext(
        intent=EventsIntent.EVENT_DISCOVERY, verification_status=VerificationStatus.VERIFIED,
        student_name="Aditi Rao", assessments=[assessment],
    )
    return provider.generate_events_response(context)


def test_unchecked_events_are_never_described_as_conflict_free() -> None:
    text = _render(_assessment(timetable=None))
    assert "no schedule conflicts" not in text
    assert "schedule conflicts NOT checked" in text


def test_checked_events_without_clashes_are_described_as_conflict_free() -> None:
    text = _render(_assessment(timetable=[]))
    assert text.startswith("Events with no schedule conflicts:")
    assert "NOT checked" not in text
