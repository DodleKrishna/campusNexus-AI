"""Phase 9 demo-reliability tests: planner failures, unsupported goals, the
real-provider planning path end-to-end, and the API's readiness/mode reporting.

No live LLM: the "real provider" test drives AnthropicLLMProvider through its
``client=`` seam with a scripted fake, so the exact production code path
(tool schema -> Pydantic validation -> MissionPlan with typed constraints ->
validator -> real agents with dependency-fact propagation) runs offline.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, Dict

from app.agents.academic.agent import AcademicAgent
from app.agents.career.agent import CareerAgent
from app.agents.events.agent import EventsAgent
from app.db.repositories.missions import get_audit_trail
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.base import LLMProviderError
from app.llm.providers.anthropic_provider import AnthropicLLMProvider
from app.schemas.enums import AgentName, MissionStatus, TaskStatus, UserRole
from app.schemas.mission import MissionPlan, MissionTask
from app.services.context import ContextService
from tests.graph_doubles import AlwaysFailAgent, FixedPlanLLMProvider, SuccessAgent

DEMO_STUDENT = "STU-DEMO-001"


def _raising_planner(exc: Exception) -> FixedPlanLLMProvider:
    def _factory(mission_id: str, goal: str) -> MissionPlan:
        raise exc

    return FixedPlanLLMProvider(plan_factory=_factory)


def _single_agent_registry(agent_cls) -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda s: agent_cls(s))
    return registry


# ---------------------------------------------------------------------------
# Planner failure: fail the mission visibly, never crash or hang in PLANNING
# ---------------------------------------------------------------------------


def test_planner_failure_fails_the_mission_with_an_audit_event(session_factory) -> None:
    llm = _raising_planner(LLMProviderError("Anthropic API call failed (APIConnectionError): Connection error."))
    orchestrator = MissionOrchestrator(session_factory=session_factory, registry=_single_agent_registry(SuccessAgent), llm_provider=llm)

    final = orchestrator.run_mission("Check my OS attendance", user_id="u1", user_role=UserRole.STUDENT)

    assert final["mission_status"] == MissionStatus.FAILED
    assert "planner could not produce a usable plan" in final["final_result"]
    assert "Connection error" in final["final_result"]
    assert "No task was executed" in final["final_result"]
    with session_factory() as session:
        context = ContextService(session)
        mission = context.get_mission(final["mission_id"])
        assert mission.status == MissionStatus.FAILED  # persisted -- not stuck in PLANNING
        assert mission.final_result == final["final_result"]
        assert context.list_agent_runs(final["mission_id"]) == []
        events = [e.event_type for e in get_audit_trail(session, final["mission_id"])]
    assert "plan_generation_failed" in events
    assert "mission_finalized" in events


def test_replan_failure_fails_the_mission_and_keeps_earlier_results(session_factory) -> None:
    calls = {"n": 0}

    def _factory(mission_id: str, goal: str) -> MissionPlan:
        calls["n"] += 1
        if calls["n"] > 1:
            raise LLMProviderError("rate limited")
        task = MissionTask(task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT, objective="do the thing")
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[task])

    orchestrator = MissionOrchestrator(
        session_factory=session_factory,
        registry=_single_agent_registry(AlwaysFailAgent),
        llm_provider=FixedPlanLLMProvider(plan_factory=_factory),
    )
    final = orchestrator.run_mission("goal", user_id="u1", user_role=UserRole.STUDENT)

    assert calls["n"] == 2  # the original plan + one replan attempt
    assert final["mission_status"] == MissionStatus.FAILED
    assert "rate limited" in final["final_result"]
    assert "Results gathered before the failure" in final["final_result"]


def test_unsupported_goal_ends_with_a_clear_explanation_and_dispatches_nothing(session_factory) -> None:
    def _factory(mission_id: str, goal: str) -> MissionPlan:
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[], unsupported_requests=["Booking flights is not supported."])

    orchestrator = MissionOrchestrator(
        session_factory=session_factory,
        registry=_single_agent_registry(SuccessAgent),
        llm_provider=FixedPlanLLMProvider(plan_factory=_factory),
    )
    final = orchestrator.run_mission("Book me a flight", user_id="u1", user_role=UserRole.STUDENT)

    assert final["mission_status"] == MissionStatus.FAILED
    assert final["final_result"] == "CampusNexus can't help with this goal: Booking flights is not supported."
    with session_factory() as session:
        assert ContextService(session).list_agent_runs(final["mission_id"]) == []


def test_partially_supported_goal_reports_what_was_not_handled(session_factory) -> None:
    def _factory(mission_id: str, goal: str) -> MissionPlan:
        task = MissionTask(task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT, objective="attendance")
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[task], unsupported_requests=["Fee payment is not supported."])

    orchestrator = MissionOrchestrator(
        session_factory=session_factory,
        registry=_single_agent_registry(SuccessAgent),
        llm_provider=FixedPlanLLMProvider(plan_factory=_factory),
    )
    final = orchestrator.run_mission("attendance and pay fees", user_id="u1", user_role=UserRole.STUDENT)

    assert final["mission_status"] == MissionStatus.COMPLETED
    assert final["final_result"].endswith("Not handled by this mission: Fee payment is not supported.")


# ---------------------------------------------------------------------------
# The real-provider planning path, end-to-end with real agents (offline)
# ---------------------------------------------------------------------------

_CAREER_PREP_PROPOSAL = {
    "tasks": [
        {"index": 1, "objective": "What is my timetable?", "agent": "academic_agent", "requires_evidence": False},
        {"index": 2, "objective": "When are my exams?", "agent": "academic_agent", "requires_evidence": False},
        {"index": 3, "objective": "Find AI internships I'm eligible for and identify my skill gaps", "agent": "career_agent"},
        {
            "index": 4,
            "objective": "Find workshops related to my skill gaps that don't conflict with my classes and exams",
            "agent": "events_opportunity_agent",
            "depends_on_indices": [1, 2, 3],
        },
    ]
}


def _scripted_anthropic_response(kwargs: Dict[str, Any]) -> SimpleNamespace:
    """Answers each structured call by its tool name, so concurrent dispatch
    order doesn't matter; free-text calls get a fixed rendering."""
    tools = kwargs.get("tools")
    if not tools:
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="(rendered from verified facts)")])
    name = tools[0]["name"]
    query = kwargs["messages"][0]["content"].lower()
    if name == "produce_mission_plan":
        payload: Dict[str, Any] = _CAREER_PREP_PROPOSAL
    elif name == "classify_academic_intent":
        payload = {"intent": "timetable" if "timetable" in query else "exam_schedule", "raw_course_reference": None}
    elif name == "classify_career_intent":
        payload = {"intent": "opportunity_discovery"}
    elif name == "classify_events_intent":
        payload = {"intent": "event_discovery"}
    else:
        raise AssertionError(f"unexpected tool {name}")
    return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", name=name, input=payload)])


def test_real_provider_plan_propagates_verified_upstream_facts_to_events(seeded_session, session_factory, knowledge_service) -> None:
    from tests.test_anthropic_provider import FakeClient

    llm = AnthropicLLMProvider(model="claude-sonnet-5", client=FakeClient(_scripted_anthropic_response))
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda s: AcademicAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.CAREER_AGENT, lambda s: CareerAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, lambda s: EventsAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    orchestrator = MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm)

    final = orchestrator.run_mission(
        "Help me prepare for AI internships without missing important classes.",
        user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT,
    )

    assert final["mission_status"] == MissionStatus.COMPLETED
    tasks = final["plan"].tasks
    assert all(final["task_status"][t.task_id] == TaskStatus.COMPLETED for t in tasks)
    career = final["agent_results"][tasks[2].task_id]
    events = final["agent_results"][tasks[3].task_id]
    skill_gaps = career.facts["skill_gaps"]
    assert skill_gaps, "the career task must publish verified skill gaps"

    assessments = events.facts["assessments"]
    assert assessments
    # Timetable/exam facts from tasks 1-2 actually reached the events task:
    assert all(a["conflict_check_performed"] for a in assessments)
    # ...and so did the career task's skill gaps -- every event returned
    # relates to at least one gap (the objective itself carries no topic words).
    gap_tokens = {tok.lower() for gap in skill_gaps for tok in re.findall(r"[A-Za-z0-9]{3,}|\b[A-Z]{2,6}\b", gap)}
    for assessment in assessments:
        event = assessment["event"]
        text = f"{event['title']} {event.get('description', '')} {event.get('category', '')}".lower()
        assert any(tok in text for tok in gap_tokens), event["title"]


# ---------------------------------------------------------------------------
# API: readiness/mode reporting, readable agent output, no 500 on planner failure
# ---------------------------------------------------------------------------


def test_health_reports_mode_database_and_policy_store(api_client) -> None:
    body = api_client.get("/health").json()
    assert body["llm"] == {"provider": "mock", "live": False, "model": None}
    assert body["database"]["seeded"] is True
    assert body["policy_store"]["available"] is True and body["policy_store"]["chunks"] > 0
    assert body["ready"] is True


def test_mission_response_includes_each_agents_readable_answer(api_client) -> None:
    response = api_client.post(
        "/missions", json={"goal": "Check my complaints and tell me whether any are overdue."},
        headers={"X-Demo-Identity": "student-demo"},
    )
    assert response.status_code == 200
    run = response.json()["agent_results"][0]
    assert run["response_text"].startswith("Your campus service cases")
    assert "_response_text" not in run["facts"]


def test_planner_failure_through_the_api_returns_a_failed_mission_not_a_500(seeded_session, session_factory, knowledge_service) -> None:
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    app = create_app(
        session_factory=session_factory,
        knowledge_service=knowledge_service,
        llm_provider=_raising_planner(LLMProviderError("Anthropic API call failed (AuthenticationError): invalid x-api-key")),
    )
    with TestClient(app) as client:
        response = client.post("/missions", json={"goal": "Check my OS attendance"}, headers={"X-Demo-Identity": "student-demo"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "invalid x-api-key" in body["final_result"]
    assert body["plan"] == [] and body["agent_results"] == []


def test_synthesis_skips_an_answer_already_contained_in_an_earlier_one(session_factory) -> None:
    def _factory(mission_id: str, goal: str) -> MissionPlan:
        tasks = [
            MissionTask(task_id=f"{mission_id}-task-{i}", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT, objective=objective)
            for i, objective in enumerate(["same answer", "same answer", "different"], start=1)
        ]
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=tasks)

    orchestrator = MissionOrchestrator(
        session_factory=session_factory,
        registry=_single_agent_registry(SuccessAgent),
        llm_provider=FixedPlanLLMProvider(plan_factory=_factory),
    )
    final = orchestrator.run_mission("goal", user_id="u1", user_role=UserRole.STUDENT)
    assert final["final_result"] == "[verified] same answer [verified] different"
