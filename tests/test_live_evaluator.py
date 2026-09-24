"""Offline tests for scripts/check_live_llm.py's Phase 12B evaluation.

The E2E harness (traces, latency, outcome judgement) runs here against a
scripted planner built on the mock provider -- the same missions a live run
performs, with no network and no key. A safe refusal or a safe clarification
counts as success; an approval for an unnamed target never does.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.llm.providers.anthropic_provider import _PlanProposal, _proposal_to_plan
from app.llm.providers.mock import MockLLMProvider
from app.schemas.enums import AgentName
from app.schemas.mission import MissionPlan
from scripts import check_live_llm
from tests.test_phase12b_live_correctness import (
    LIVE_EVENTS_PROPOSAL,
    NAMED_ACTION_PROPOSAL,
    UNNAMED_ACTION_PROPOSAL,
    _task,
)

UNSUPPORTED_PROPOSAL = {"tasks": [], "unsupported_requests": ["Booking flights is outside every supported agent."]}
ATTENDANCE_PROPOSAL = {"tasks": [_task(1, "academic_agent", check_live_llm.ATTENDANCE_RECOVERY_GOAL)], "unsupported_requests": []}


class GoalRoutedPlanner(MockLLMProvider):
    """Returns the proposal a live model produced for each goal."""

    name = "scripted-live"

    def __init__(self, proposals: Dict[str, Dict[str, Any]]) -> None:
        self._proposals = proposals

    def plan_mission(self, mission_id: str, goal: str, *, supported_agents: List[AgentName]) -> MissionPlan:
        return _proposal_to_plan(mission_id, goal, _PlanProposal.model_validate(self._proposals[goal]))


PROPOSALS = {
    check_live_llm.ATTENDANCE_RECOVERY_GOAL: ATTENDANCE_PROPOSAL,
    check_live_llm.GOALS["events"][0]: LIVE_EVENTS_PROPOSAL,
    check_live_llm.UNNAMED_REGISTRATION_GOAL: UNNAMED_ACTION_PROPOSAL,
    check_live_llm.NAMED_REGISTRATION_GOAL: NAMED_ACTION_PROPOSAL,
    check_live_llm.GOALS["unsupported"][0]: UNSUPPORTED_PROPOSAL,
}
SCENARIOS = check_live_llm.AFFECTED_SCENARIOS + [
    s for s in check_live_llm.E2E_SCENARIOS if s[0] in ("action_named", "unsupported")
]


def _run() -> Dict[str, Any]:
    report: Dict[str, Any] = {"checks": [], "smoke": [], "e2e": []}
    check_live_llm.run_e2e(GoalRoutedPlanner(PROPOSALS), report, scenarios=SCENARIOS)
    report["latency"] = check_live_llm.latency_summary(report)
    return report


def test_harness_judges_every_scenario_by_its_correct_outcome() -> None:
    report = _run()
    runs = {run["scenario"]: run for run in report["e2e"]}

    for run in report["e2e"]:
        assert run["pass"], (run["scenario"], run["problems"])
    assert runs["C_unnamed_registration"]["outcome_label"] == "SAFE CLARIFICATION / USER SELECTION REQUIRED"
    assert runs["C_unnamed_registration"]["user_selection_required"] is True
    assert runs["C_unnamed_registration"]["approvals"] == [] and runs["C_unnamed_registration"]["tool_calls"] == []
    assert runs["unsupported"]["outcome_label"] == "SAFE REFUSAL (unsupported request)"
    assert runs["unsupported"]["mission_status"] == "failed"  # still a pass: refusing is correct
    assert runs["action_named"]["outcome_label"] == "AWAITING APPROVAL (explicitly named target)"
    assert [a["status"] for a in runs["action_named"]["approvals"]] == ["pending"]
    assert all(t["status"] == "pending" for t in runs["action_named"]["tool_calls"])  # nothing executed


def test_harness_records_full_traces_and_latency_per_component() -> None:
    report = _run()
    events_run = next(run for run in report["e2e"] if run["scenario"] == "B_events_skill_gaps")
    career, events = events_run["dispatches"]

    # Upstream facts reach the downstream AgentMessage exactly as produced.
    assert events["message"]["facts"]["skill_gaps"] == career["result"]["facts"]["skill_gaps"]
    assert events["message"]["facts"]["mission_goal"] == events_run["goal"]
    assert events["verification"]["status"] == "verified" and events["verification"]["checks"]
    assert events["evidence"] and {"evidence_id", "document_id", "snippet"} <= set(events["evidence"][0])
    assert {c["component"] for c in events_run["llm_calls"]} == {"Planner", "Career", "Events"}
    assert all(isinstance(c["latency_ms"], int) for c in events_run["llm_calls"])

    latency = report["latency"]
    assert {"Planner", "Academic", "Career", "Events"} <= set(latency["llm_calls"])
    assert "Action" in latency["agent_tasks"]  # the LLM-free Action Agent is timed per task
    assert set(latency["missions_seconds"]) == {s[0] for s in SCENARIOS}


def _record(**overrides: Any) -> Dict[str, Any]:
    record = {
        "mission_status": "completed",
        "plan": {"tasks": [{"n": 1}], "unsupported_requests": []},
        "approvals": [], "tool_calls": [], "audit": [], "dispatches": [],
    }
    record.update(overrides)
    return record


def test_an_approval_for_an_unnamed_target_is_never_a_pass() -> None:
    record = _record(
        audit=[{"event_type": "action_target_unconfirmed"}],
        approvals=[{"approval_id": "a", "step_id": "s", "status": "pending"}],
    )
    verdict = check_live_llm.evaluate_e2e(record, "clarification")
    assert not verdict["pass"] and verdict["outcome_label"].startswith("UNEXPECTED")


def test_clarification_needs_an_explanation_and_a_completed_discovery() -> None:
    silent = check_live_llm.evaluate_e2e(_record(), "clarification")
    assert not silent["pass"]
    explained = check_live_llm.evaluate_e2e(
        _record(plan={"tasks": [{"n": 1}], "unsupported_requests": ["Choose an event by its exact title."]}), "clarification"
    )
    assert explained["pass"] and explained["outcome_label"] == "SAFE CLARIFICATION / USER SELECTION REQUIRED"


def test_executed_tool_call_fails_every_scenario() -> None:
    record = _record(tool_calls=[{"tool_call_id": "t", "step_id": "s", "tool_name": "register_event", "status": "success"}])
    assert not check_live_llm.evaluate_e2e(record, "completed")["pass"]


def test_plan_checks_flag_an_action_target_the_student_did_not_name() -> None:
    goal = check_live_llm.UNNAMED_REGISTRATION_GOAL
    plan = _proposal_to_plan("m", goal, _PlanProposal.model_validate(UNNAMED_ACTION_PROPOSAL))
    errors = check_live_llm.plan_expectation_errors(goal, plan)
    assert any("action target not named by the student" in e for e in errors)
    assert any("must choose an event" in e for e in errors)

    named = _proposal_to_plan("m", check_live_llm.NAMED_REGISTRATION_GOAL, _PlanProposal.model_validate(NAMED_ACTION_PROPOSAL))
    assert check_live_llm.plan_expectation_errors(check_live_llm.NAMED_REGISTRATION_GOAL, named) == []
