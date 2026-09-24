"""Phase 14 -- presentation-state logic and the small API additions behind it.

The presenters only rephrase real API data, so they are tested against real
payloads from the offline API (mock LLM), not hand-made dictionaries: the
multi-agent progress strip, the selection-to-execution stages, the mission
state a student sees first (including a live-provider outage), the verified
action result, and the new read-only fields/endpoint.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.llm.base import LLMRateLimitError
from app.llm.providers.mock import MockLLMProvider
from app.tools.build import build_default_tool_registry
from streamlit_app import api_client as api_client_module
from streamlit_app import presenters

STUDENT = {"X-Demo-Identity": "student-demo"}
OTHER = {"X-Demo-Identity": "student-alt"}
ADMIN = {"X-Demo-Identity": "admin-demo"}
CAREER_GOAL = (
    "I'm a third-year CSE student interested in AI. Find internships I'm eligible for, identify my skill gaps, "
    "find relevant workshops that don't conflict with my classes, and create a preparation plan."
)
ATTENDANCE_GOAL = (
    "Check my Operating Systems attendance, determine whether I currently meet the attendance requirement, and "
    "explain how many classes I need to attend to reach the required attendance."
)
SELECT_GOAL = "Find a suitable event, check my schedule and prepare my registration."
APP_PATH = str(Path(__file__).resolve().parents[1] / "streamlit_app" / "app.py")


def _stages(mission: Dict, candidates=None) -> Dict[str, str]:
    return {s.label: s.status for s in presenters.progress_stages(mission, candidates)}


def _mission(api_client, goal: str) -> Dict:
    response = api_client.post("/missions", json={"goal": goal}, headers=STUDENT)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Presenters over real payloads
# ---------------------------------------------------------------------------


def test_multi_agent_progress_names_each_verified_stage(api_client) -> None:
    mission = _mission(api_client, CAREER_GOAL)

    stages = presenters.progress_stages(mission)

    assert [(s.label, s.status) for s in stages] == [
        ("Academic Schedule", "VERIFIED"), ("Academic Exams", "VERIFIED"),
        ("Career Analysis", "VERIFIED"), ("Event Matching", "VERIFIED"),
    ]
    assert presenters.mission_state(mission).label == "COMPLETED"
    runs = presenters.latest_runs(mission)
    events_run = next(r for r in runs.values() if r["agent"] == "events_opportunity_agent")
    assert presenters.verification_summary(events_run, mission).endswith(
        "checked for schedule conflicts against 4 timetable slots and 4 exams; matched to your skill gaps."
    )
    career_run = next(r for r in runs.values() if r["agent"] == "career_agent")
    assert presenters.verification_summary(career_run, mission).startswith("Eligibility calculated from current student records")


def test_policy_backed_answer_names_its_source_and_threshold(api_client) -> None:
    mission = _mission(api_client, ATTENDANCE_GOAL)
    run = mission["agent_results"][0]

    summary = presenters.verification_summary(run, mission)

    assert summary.startswith("Attendance calculated from current student records (34/50 classes)")
    assert "required 75% from Attendance Policy (2025-26) (v2)" in summary
    assert presenters.evidence_sources(run["evidence"])[0] == "Attendance Policy (2025-26) (v2)"
    labels = [s.label for s in presenters.progress_stages(mission)]
    assert len(labels) == len(set(labels))  # two attendance tasks stay distinguishable
    assert labels[0] == "Attendance Check (T1)"


def test_selection_mission_stages_from_discovery_to_verified_execution(api_client) -> None:
    mission = _mission(api_client, SELECT_GOAL)
    mission_id = mission["mission_id"]
    candidates = api_client.get(f"/missions/{mission_id}/candidates", headers=STUDENT).json()

    state = presenters.mission_state(mission)
    assert state.key == "user_selection" and state.label == "USER SELECTION REQUIRED"
    assert "Choose the event you want to register for" in state.message
    before = _stages(mission, candidates)
    assert before["Event Discovery"] == "VERIFIED" and before["Schedule Check"] == "VERIFIED"
    assert before["Student Selection"] == "WAITING" and "Approval" not in before

    selected = api_client.post(
        f"/missions/{mission_id}/selection", json={"resource_type": "event", "resource_id": 10}, headers=STUDENT
    ).json()["mission"]
    waiting = _stages(selected, api_client.get(f"/missions/{mission_id}/candidates", headers=STUDENT).json())
    assert waiting["Student Selection"] == "COMPLETED" and waiting["Action Pre-check"] == "VERIFIED"
    assert waiting["Approval"] == "WAITING" and waiting["Execution"] == "NOT STARTED"
    assert presenters.mission_state(selected).label == "WAITING FOR APPROVAL"
    assert presenters.action_result(selected) is None  # nothing has executed

    approval_id = api_client.get("/approvals/pending", headers=ADMIN).json()[0]["approval_id"]
    api_client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers=ADMIN)
    done = api_client.post(f"/missions/{mission_id}/resume", headers=STUDENT).json()

    final = _stages(done)
    assert final["Approval"] == "APPROVED" and final["Execution"] == "VERIFIED"
    result = presenters.action_result(done)
    assert result.verified and result.headline == "ACTION VERIFIED"
    assert result.title == "Registration successful" and result.target == "Competitive Coding Contest"
    assert result.rows == [("Pre-execution check", "VERIFIED"), ("Database write", "SUCCESS"), ("Postcondition check", "VERIFIED")]


def test_candidate_presentation_is_short_and_grouped(api_client) -> None:
    mission_id = _mission(api_client, SELECT_GOAL)["mission_id"]
    listing = api_client.get(f"/missions/{mission_id}/candidates", headers=STUDENT).json()

    selectable, blocked, not_open = presenters.split_candidates(listing["candidates"])

    assert [c["title"] for c in selectable] == ["Competitive Coding Contest", "Photography Contest Exhibition"]
    hackathon = next(c for c in blocked if c["resource_id"] == 2)
    assert presenters.candidate_reason(hackathon) == "Clashes with CS301 class"
    assert all(c["assessment"]["status"] == "unavailable" for c in not_open)
    assert presenters.fmt_when("2026-10-03T09:30:00Z", "2026-10-03T12:30:00Z") == "Sat 03 Oct 2026, 15:00–18:00 IST"


def test_working_message_follows_real_step_state() -> None:
    assert presenters.working_message(None) == "Creating mission plan…"
    running = {"running_step": {"agent": "career_agent"}, "steps_total": 4, "steps_done": 2}
    assert presenters.working_message(running) == "Career Agent evaluating opportunities…"
    assert presenters.working_message({"running_step": None, "steps_total": 3, "steps_done": 3}) == "Verifier checking results…"


# ---------------------------------------------------------------------------
# API additions
# ---------------------------------------------------------------------------


def test_student_missions_endpoint_lists_newest_first_and_enforces_ownership(api_client) -> None:
    first = _mission(api_client, ATTENDANCE_GOAL)["mission_id"]
    second = _mission(api_client, SELECT_GOAL)["mission_id"]

    rows = api_client.get("/students/STU-DEMO-001/missions", headers=STUDENT).json()

    assert [r["mission_id"] for r in rows[:2]] == [second, first]
    assert rows[0]["user_selection_required"] is True and rows[0]["running_step"] is None
    assert rows[0]["steps_total"] == rows[0]["steps_done"] == 3
    assert api_client.get("/students/STU-DEMO-001/missions", headers=OTHER).status_code == 403
    assert api_client.get("/students/STU-DEMO-001/missions", headers=ADMIN).status_code == 200


def test_health_reports_whether_live_ai_is_configured_without_the_key(api_client, monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert api_client.get("/health").json()["live_ai_configured"]["groq"] is False
    monkeypatch.setenv("GROQ_API_KEY", "gsk-secret-value")
    body = api_client.get("/health").text
    assert '"groq":true' in body.replace(" ", "") and "gsk-secret-value" not in body


def test_approval_card_carries_the_selected_target_title(api_client) -> None:
    mission_id = _mission(api_client, SELECT_GOAL)["mission_id"]
    api_client.post(f"/missions/{mission_id}/selection", json={"resource_type": "event", "resource_id": 10}, headers=STUDENT)

    card = api_client.get("/approvals/pending", headers=ADMIN).json()[0]

    assert card["target_title"] == "Competitive Coding Contest" and card["target_source"] == "user_selection"


class _RateLimitedPlanner(MockLLMProvider):
    name = "groq"
    is_live = True
    model_name = "openai/gpt-oss-120b"

    def plan_mission(self, mission_id, goal, *, supported_agents):
        raise LLMRateLimitError("429 Too Many Requests", provider="groq", model=self.model_name, status_code=429, retry_after_seconds=12)


@pytest.fixture()
def outage_client(seeded_session, session_factory, knowledge_service):
    app = create_app(
        session_factory=session_factory, knowledge_service=knowledge_service,
        llm_provider=_RateLimitedPlanner(), tool_gateway=build_default_tool_registry(),
    )
    with TestClient(app) as client:
        yield client


def test_provider_outage_is_a_distinct_state_not_a_traceback(outage_client) -> None:
    mission = _mission(outage_client, CAREER_GOAL)

    assert mission["status"] == "failed"
    assert mission["provider_unavailable"] == {
        "provider": "groq", "model": "openai/gpt-oss-120b", "kind": "rate_limit", "status_code": 429, "retry_after_seconds": 12,
    }
    state = presenters.mission_state(mission)
    assert state.label == "PROVIDER UNAVAILABLE" and "Live AI is temporarily unavailable" in state.message
    assert "Traceback" not in state.message


# ---------------------------------------------------------------------------
# UI (Streamlit AppTest against the real API, offline)
# ---------------------------------------------------------------------------

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest


@pytest.fixture()
def offline_transport(api_app):
    with TestClient(api_app) as client:
        api_client_module.set_default_http_client(client)
        yield client
    api_client_module.set_default_http_client(None)


def _app():
    at = AppTest.from_file(APP_PATH)
    at.run(timeout=30)
    return at


def test_starter_button_runs_with_progress_and_shows_the_selection_state(offline_transport) -> None:
    at = _app()
    at.sidebar.radio[0].set_value("Mission Workspace").run(timeout=30)
    starter = next(b for b in at.button if b.label == "🗓️ Choose an Event & Register")

    starter.click().run(timeout=90)

    assert not at.exception
    body = "\n".join(md.value for md in at.markdown)
    assert ">USER SELECTION REQUIRED<" in body and "Student Selection" in body and ">WAITING<" in body
    assert any("Choose the event you want to register for" in i.value for i in at.info)
    assert "#### Choose an event" in body
    assert len([b for b in at.button if b.label == "Select this event"]) == 2


def test_completed_action_shows_action_verified(offline_transport) -> None:
    mission_id = offline_transport.post("/missions", json={"goal": SELECT_GOAL}, headers=STUDENT).json()["mission_id"]
    offline_transport.post(f"/missions/{mission_id}/selection", json={"resource_type": "event", "resource_id": 10}, headers=STUDENT)
    approval_id = offline_transport.get("/approvals/pending", headers=ADMIN).json()[0]["approval_id"]
    offline_transport.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers=ADMIN)
    offline_transport.post(f"/missions/{mission_id}/resume", headers=STUDENT)

    at = AppTest.from_file(APP_PATH)
    at.session_state["cn_current_mission_id"] = mission_id
    at.run(timeout=30)
    at.sidebar.radio[0].set_value("Mission Workspace").run(timeout=30)

    assert not at.exception
    body = "\n".join(md.value for md in at.markdown)
    assert "ACTION VERIFIED" in body and "Registration successful" in body and "Competitive Coding Contest" in body
    assert "Postcondition check" in body and ">COMPLETED<" in body


def test_sidebar_says_deterministic_demo_mode_and_dashboard_lists_active_work(offline_transport) -> None:
    offline_transport.post("/missions", json={"goal": SELECT_GOAL}, headers=STUDENT)

    at = _app()

    assert not at.exception
    sidebar = "\n".join(md.value for md in at.sidebar.markdown)
    assert "DETERMINISTIC DEMO MODE" in sidebar and "LIVE AI MODE" not in sidebar
    body = "\n".join(md.value for md in at.markdown)
    assert "Active missions & actions" in body and ">USER SELECTION REQUIRED<" in body
    assert "Attendance risk" in body
