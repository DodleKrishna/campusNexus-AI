"""Post-12C: ``user_selection_required`` reflects the mission's structured state.

A live planner may handle an unnamed registration in two ways: plan an
action task with no target (refused by target provenance), or plan only the
discovery tasks and declare the action in ``selection_required_actions``.
Both must report ``user_selection_required=True`` -- derived from the plan and
target provenance, never from the final response's prose -- and neither may
ever produce an approval, an Action Agent run or a tool call.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.db.models.mission import ApprovalRecord
from app.graph.orchestrator import user_selection_required
from app.schemas.enums import AgentName, ApprovalStatus, MissionStatus
from app.schemas.mission import MissionPlan, MissionTask
from scripts import check_live_llm
from tests.test_phase12b_live_correctness import (
    CANDIDATE_OBJECTIVE,
    NAMED_ACTION_PROPOSAL,
    NAMED_GOAL,
    ScriptedLivePlanner,
    _assert_nothing_proposed,
    _orchestrator,
    _rows,
    _run,
    _task,
)

STUDENT = {"X-Demo-Identity": "student-demo"}
ADMIN = {"X-Demo-Identity": "admin-demo"}
UNNAMED_GOAL = check_live_llm.UNNAMED_REGISTRATION_GOAL
NAMED_API_GOAL = (
    "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
RECOMMENDATION_GOAL = "Are there any workshops this month that don't clash with my classes or exams?"
FLIGHT_GOAL = check_live_llm.GOALS["unsupported"][0]

# The exact shape the live Groq Case C run produced, plus the structured declaration.
LIVE_CASE_C_PROPOSAL: Dict[str, Any] = {
    "tasks": [
        _task(1, "academic_agent", "Provide the student's weekly class timetable."),
        _task(2, "academic_agent", "Provide the student's exam schedule."),
        _task(3, "events_opportunity_agent", CANDIDATE_OBJECTIVE, deps=[1, 2]),
    ],
    "unsupported_requests": ["The student did not provide the exact event title; they must select one event first."],
    "selection_required_actions": ["register_event"],
}


def _selection_events(session_factory, mission_id: str) -> List[dict]:
    from app.services.context import ContextService

    with session_factory() as session:
        return [
            dict(e.event_metadata or {})
            for e in ContextService(session).list_audit_events(mission_id)
            if e.event_type == "action_target_unconfirmed"
        ]


# ---------------------------------------------------------------------------
# API (offline mock planner): the flag the UI consumes
# ---------------------------------------------------------------------------


def _create(api_client, goal: str) -> dict:
    response = api_client.post("/missions", json={"goal": goal}, headers=STUDENT)
    assert response.status_code == 200, response.text
    return response.json()


def test_unnamed_event_registration_requires_user_selection(api_client) -> None:
    body = _create(api_client, UNNAMED_GOAL)

    assert body["status"] == MissionStatus.COMPLETED.value
    assert body["user_selection_required"] is True
    assert body["pending_approvals"] == []
    assert not any(r["agent"] == AgentName.ACTION_AGENT.value for r in body["agent_results"])
    assert not any(t["agent"] == AgentName.ACTION_AGENT.value for t in body["plan"])
    # Fetching the mission again reports the same flag from persisted state.
    again = api_client.get(f"/missions/{body['mission_id']}", headers=STUDENT).json()
    assert again["user_selection_required"] is True
    timeline = api_client.get(f"/missions/{body['mission_id']}/timeline", headers=STUDENT).json()
    assert [e["status"] for e in timeline["entries"] if e["event_type"] == "action_target_unconfirmed"] == ["NEEDS_REVIEW"]


def test_explicitly_named_registration_does_not_require_selection(api_client) -> None:
    body = _create(api_client, NAMED_API_GOAL)

    assert body["status"] == MissionStatus.NEEDS_APPROVAL.value
    assert body["user_selection_required"] is False
    assert len(body["pending_approvals"]) == 1


def test_completed_approved_action_does_not_require_selection(api_client) -> None:
    body = _create(api_client, NAMED_API_GOAL)
    approval_id = body["pending_approvals"][0]["approval_id"]
    decided = api_client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers=ADMIN)
    assert decided.status_code == 200, decided.text

    resumed = api_client.post(f"/missions/{body['mission_id']}/resume", headers=STUDENT).json()
    assert resumed["status"] == MissionStatus.COMPLETED.value
    assert resumed["user_selection_required"] is False


def test_recommendation_only_event_search_does_not_require_selection(api_client) -> None:
    body = _create(api_client, RECOMMENDATION_GOAL)

    assert body["status"] == MissionStatus.COMPLETED.value
    assert body["user_selection_required"] is False


def test_unsupported_flight_request_does_not_require_selection(api_client) -> None:
    body = _create(api_client, FLIGHT_GOAL)

    assert body["plan"] == []
    assert body["user_selection_required"] is False


def test_informational_mission_does_not_require_selection(api_client) -> None:
    body = _create(api_client, "What is my attendance in Operating Systems?")
    assert body["user_selection_required"] is False


# ---------------------------------------------------------------------------
# Live-shaped plans through the real Orchestrator (scripted planner, offline)
# ---------------------------------------------------------------------------


def test_live_case_c_shape_requires_selection_without_any_action(session_factory, seeded_session, knowledge_service) -> None:
    final = _run(_orchestrator(session_factory, knowledge_service, ScriptedLivePlanner(LIVE_CASE_C_PROPOSAL)), UNNAMED_GOAL)
    mission_id = final["mission_id"]

    assert final["mission_status"] == MissionStatus.COMPLETED
    assert user_selection_required(final["plan"], UNNAMED_GOAL) is True
    _assert_nothing_proposed(session_factory, mission_id)  # zero approvals, tool calls, Action Agent runs
    assert _selection_events(session_factory, mission_id) == [
        {"tool_name": "register_event", "source": "planner", "user_selection_required": True}
    ]


def test_live_harness_reports_the_flag_for_the_live_case_c_shape() -> None:
    from tests.test_live_evaluator import PROPOSALS, GoalRoutedPlanner

    report: Dict[str, Any] = {"checks": [], "smoke": [], "e2e": []}
    proposals = dict(PROPOSALS, **{UNNAMED_GOAL: LIVE_CASE_C_PROPOSAL})
    scenarios = [s for s in check_live_llm.AFFECTED_SCENARIOS if s[0] == "C_unnamed_registration"]
    check_live_llm.run_e2e(GoalRoutedPlanner(proposals), report, scenarios=scenarios)

    run = report["e2e"][0]
    assert run["pass"], run["problems"]
    assert run["outcome_label"] == "SAFE CLARIFICATION / USER SELECTION REQUIRED"
    assert run["user_selection_required"] is True
    assert run["approvals"] == [] and run["tool_calls"] == [] and run["replans"] == 0


def test_named_target_overrides_a_spurious_declaration(session_factory, seeded_session, knowledge_service) -> None:
    proposal = dict(NAMED_ACTION_PROPOSAL, selection_required_actions=["register_event"])
    final = _run(_orchestrator(session_factory, knowledge_service, ScriptedLivePlanner(proposal)), NAMED_GOAL)

    assert final["mission_status"] == MissionStatus.NEEDS_APPROVAL
    assert user_selection_required(final["plan"], NAMED_GOAL) is False
    assert _selection_events(session_factory, final["mission_id"]) == []
    assert [a.status for a in _rows(session_factory, ApprovalRecord, final["mission_id"])] == [ApprovalStatus.PENDING]


# ---------------------------------------------------------------------------
# The deterministic helper
# ---------------------------------------------------------------------------


def _plan(tasks: List[MissionTask], selections: List[str]) -> MissionPlan:
    return MissionPlan(mission_id="m", goal="g", tasks=tasks, selection_required_actions=selections)


def _action(constraints: Dict[str, Any]) -> MissionTask:
    return MissionTask(task_id="m-task-1", mission_id="m", agent=AgentName.ACTION_AGENT, objective="act", constraints=constraints)


def test_helper_uses_only_structured_plan_state() -> None:
    goal = "Register me for Competitive Coding Contest."
    assert user_selection_required(None, goal) is False
    assert user_selection_required(_plan([], []), goal) is False
    assert user_selection_required(_plan([], ["register_event"]), goal) is True
    assert user_selection_required(_plan([_action({"tool_name": "register_event"})], []), goal) is True
    named = _action({"tool_name": "register_event", "event_title": "Competitive Coding Contest"})
    assert user_selection_required(_plan([named], []), goal) is False
    assert user_selection_required(_plan([named], ["register_event"]), goal) is False
    # Prose alone never sets the flag.
    prose_only = MissionPlan(mission_id="m", goal="g", unsupported_requests=["User selection required: pick an event."])
    assert user_selection_required(prose_only, goal) is False
