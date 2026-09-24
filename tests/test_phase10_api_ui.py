"""Phase 10 -- the API/UI surface of the refined mission & action flow.

API: the approval card carries the pre-check's own verdict (VERIFIED for a
conflict-free registration) and where its schedule data came from; a mission
stopped for a repeated failure carries an ``execution_stop`` notice, flags the
identical repeat run, and shows the stop in the timeline.

UI: the Mission Workspace shows the concise stop message and folds identical
repeat runs; the Action Center renders the VERIFIED pre-check. Rendered via
streamlit.testing.v1.AppTest against the real FastAPI app in-process (same
offline harness as tests/test_streamlit_app.py).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.graph.orchestrator import DUPLICATE_FAILURE_MESSAGE
from streamlit_app import api_client as api_client_module

STUDENT = {"X-Demo-Identity": "student-demo"}
ADMIN = {"X-Demo-Identity": "admin-demo"}
REGISTER_GOAL = (
    "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
FULL_EVENT_GOAL = (
    "Find the workshop titled 'Startup Pitch Night', verify there are no conflicts with my classes or "
    "exams, and register me for it."
)
APP_PATH = str(Path(__file__).resolve().parents[1] / "streamlit_app" / "app.py")


def _pending_for(api_client, mission_id: str) -> list:
    return [a for a in api_client.get("/approvals/pending", headers=ADMIN).json() if a["mission_id"] == mission_id]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_approval_card_reports_a_verified_precheck_and_its_schedule_source(api_client) -> None:
    mission = api_client.post("/missions", json={"goal": REGISTER_GOAL}, headers=STUDENT).json()
    assert mission["status"] == "needs_approval"

    card = _pending_for(api_client, mission["mission_id"])[0]
    assert card["precheck_status"] == "verified"
    assert card["precheck_issues"] == []
    assert card["schedule_check"]["performed"] is True
    assert card["schedule_check"]["source"] == "upstream_academic_tasks"
    assert card["schedule_check"]["timetable_conflicts"] == [] and card["schedule_check"]["exam_conflicts"] == []

    # The plan the student sees has the three upstream tasks feeding the action.
    action = next(t for t in mission["plan"] if t["agent"] == "action_agent")
    upstream_agents = sorted(t["agent"] for t in mission["plan"] if t["task_id"] in action["dependencies"])
    assert upstream_agents == ["academic_agent", "academic_agent", "events_opportunity_agent"]


def test_repeated_failure_is_reported_as_an_execution_stop(api_client) -> None:
    mission = api_client.post("/missions", json={"goal": FULL_EVENT_GOAL}, headers=STUDENT).json()

    assert mission["status"] == "failed"
    stop = mission["execution_stop"]
    assert stop["reason"] == "duplicate_failure"
    assert stop["message"] == DUPLICATE_FAILURE_MESSAGE
    action_task_id = next(t["task_id"] for t in mission["plan"] if t["agent"] == "action_agent")
    assert stop["task_ids"] == [action_task_id]

    action_runs = [r for r in mission["agent_results"] if r["task_id"] == action_task_id]
    assert [r["identical_to_previous_run"] for r in action_runs] == [False, True]
    assert mission["final_result"].startswith(DUPLICATE_FAILURE_MESSAGE)


def test_successful_mission_has_no_execution_stop(api_client) -> None:
    mission = api_client.post("/missions", json={"goal": REGISTER_GOAL}, headers=STUDENT).json()
    assert mission["execution_stop"] is None
    assert not any(r["identical_to_previous_run"] for r in mission["agent_results"])


def test_timeline_shows_duplicate_failure_detection_as_failed(api_client) -> None:
    mission = api_client.post("/missions", json={"goal": FULL_EVENT_GOAL}, headers=STUDENT).json()
    timeline = api_client.get(f"/missions/{mission['mission_id']}/timeline", headers=STUDENT).json()
    by_type = {e["event_type"]: e for e in timeline["entries"]}
    assert by_type["duplicate_failure_detected"]["status"] == "FAILED"
    assert by_type["replan_triggered"]["status"] == "RUNNING"


# ---------------------------------------------------------------------------
# UI helpers (pure)
# ---------------------------------------------------------------------------


def test_identical_repeat_runs_fold_into_one_card() -> None:
    from streamlit_app.sections.mission_workspace import _run_cards

    runs = [
        {"task_id": "t1", "identical_to_previous_run": False, "status": "success"},
        {"task_id": "t2", "identical_to_previous_run": False, "status": "failed"},
        {"task_id": "t2", "identical_to_previous_run": True, "status": "failed"},
    ]
    labels = [label for _, label in _run_cards(runs, {"t1": "T1", "t2": "T2"})]
    assert labels == ["T1", "T2 · same result on 2 attempts"]


def test_distinct_runs_of_one_task_are_still_both_shown() -> None:
    from streamlit_app.sections.mission_workspace import _run_cards

    runs = [
        {"task_id": "t1", "identical_to_previous_run": False, "status": "partial"},  # proposal, awaiting approval
        {"task_id": "t1", "identical_to_previous_run": False, "status": "success"},  # executed after approval
    ]
    labels = [label for _, label in _run_cards(runs, {"t1": "T1"})]
    assert labels == ["T1 (run 1 of 2)", "T1 (run 2 of 2)"]


def test_schedule_check_note_only_states_what_was_persisted() -> None:
    from streamlit_app.sections.action_center import _schedule_check_note

    assert _schedule_check_note(None) == ""
    assert "NOT checked" in _schedule_check_note({"performed": False})
    note = _schedule_check_note({
        "performed": True, "source": "upstream_academic_tasks", "timetable_entries_checked": 5,
        "exam_entries_checked": 2, "timetable_conflicts": [], "exam_conflicts": [],
    })
    assert "5 weekly class slot(s) and 2 exam(s)" in note and "no clash found" in note


# ---------------------------------------------------------------------------
# UI rendering (AppTest)
# ---------------------------------------------------------------------------

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest


@pytest.fixture()
def offline_transport(api_app):
    with TestClient(api_app) as client:
        api_client_module.set_default_http_client(client)
        yield client
    api_client_module.set_default_http_client(None)


def test_mission_workspace_renders_the_duplicate_failure_stop(offline_transport) -> None:
    mission = offline_transport.post("/missions", json={"goal": FULL_EVENT_GOAL}, headers=STUDENT).json()

    at = AppTest.from_file(APP_PATH)
    at.session_state["cn_current_mission_id"] = mission["mission_id"]
    at.run(timeout=30)
    at.sidebar.radio[0].set_value("Mission Workspace").run(timeout=30)
    assert not at.exception

    errors = [e.value for e in at.error]
    assert any(DUPLICATE_FAILURE_MESSAGE in e for e in errors)
    expander_labels = [e.label for e in at.expander]
    assert any("same result on 2 attempts" in label for label in expander_labels)


def test_action_center_renders_a_verified_precheck(offline_transport) -> None:
    offline_transport.post("/missions", json={"goal": REGISTER_GOAL}, headers=STUDENT)

    at = AppTest.from_file(APP_PATH)
    at.run(timeout=30)
    at.sidebar.selectbox[0].set_value("admin-demo").run(timeout=30)
    at.sidebar.radio[0].set_value("Action Center").run(timeout=30)
    assert not at.exception

    body = "\n".join(md.value for md in at.markdown)
    assert "Deterministic pre-check" in body and ">VERIFIED<" in body
    assert not [w.value for w in at.warning if "Pre-check flagged" in w.value]
    assert any("no clash found" in c.value for c in at.caption)
