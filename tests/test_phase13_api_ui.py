"""Phase 13 -- the API/UI surface of candidate selection.

API: candidates are listed with deterministic statuses; a selection carries
only the resource identity, is validated server-side (ownership, candidate
membership, current validity) and moves the same mission to WAITING FOR
APPROVAL; repeats are idempotent. UI: the Mission Workspace shows the
"Choose an event" cards with selection enabled only for eligible events, and
the Action Center shows that the target was selected by the student.
A separate test proves the candidate list and selection survive real process
restarts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models.academic import Enrollment, Exam
from app.db.models.events import Event, EventRegistration
from app.db.models.identity import Student
from app.db.session import create_db_engine, create_session_factory, init_db
from app.rag.config import get_rag_config
from app.rag.embeddings import DeterministicHashEmbedding
from app.rag.ingest import ingest_policy_directory
from app.rag.vector_store import PolicyVectorStore
from scripts.seed_data import run_seed
from streamlit_app import api_client as api_client_module

STUDENT = {"X-Demo-Identity": "student-demo"}
OTHER_STUDENT = {"X-Demo-Identity": "student-alt"}
ADMIN = {"X-Demo-Identity": "admin-demo"}
GOAL = "Find a suitable event, check my schedule and prepare my registration."
CODING, PHOTO, HACKATHON = 10, 14, 2
REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = str(REPO_ROOT / "streamlit_app" / "app.py")
HELPER = REPO_ROOT / "scripts" / "_selection_subprocess_helper.py"


def _discover(api_client) -> dict:
    response = api_client.post("/missions", json={"goal": GOAL}, headers=STUDENT)
    assert response.status_code == 200, response.text
    return response.json()


def _select(api_client, mission_id: str, event_id: int, headers=STUDENT, **extra):
    body = {"resource_type": "event", "resource_id": event_id, "action": "register_event", **extra}
    return api_client.post(f"/missions/{mission_id}/selection", json=body, headers=headers)


def _pending(api_client, mission_id: str) -> list:
    return [a for a in api_client.get("/approvals/pending", headers=ADMIN).json() if a["mission_id"] == mission_id]


def _registrations(session_factory, event_id: int) -> int:
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == "STU-DEMO-001")).scalar_one()
        return session.execute(
            select(func.count()).select_from(EventRegistration).where(
                EventRegistration.event_id == event_id, EventRegistration.student_id == student.id
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_candidates_are_listed_with_deterministic_statuses(api_client) -> None:
    mission = _discover(api_client)
    assert mission["status"] == "completed" and mission["user_selection_required"] is True
    assert mission["selected_target"] is None

    listing = api_client.get(f"/missions/{mission['mission_id']}/candidates", headers=STUDENT).json()

    assert listing["user_selection_required"] is True and listing["selection_open"] is True
    by_id = {c["resource_id"]: c for c in listing["candidates"]}
    assert by_id[CODING]["selectable"] is True and by_id[CODING]["assessment"]["status"] == "eligible"
    assert by_id[HACKATHON]["selectable"] is False and by_id[HACKATHON]["assessment"]["status"] == "conflict"
    assert any("CS301" in reason for reason in by_id[HACKATHON]["assessment"]["reasons"])
    assert by_id[CODING]["title"] == "Competitive Coding Contest" and by_id[CODING]["venue"]
    # Staff may view a student's candidates; another student may not.
    assert api_client.get(f"/missions/{mission['mission_id']}/candidates", headers=ADMIN).status_code == 200
    assert api_client.get(f"/missions/{mission['mission_id']}/candidates", headers=OTHER_STUDENT).status_code == 403


def test_selection_moves_the_same_mission_to_waiting_for_approval(api_client, session_factory) -> None:
    mission_id = _discover(api_client)["mission_id"]

    # A client-supplied title is ignored: the server reloads the canonical event.
    response = _select(api_client, mission_id, CODING, title="Something Else")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == "selected" and body["message"] == "Selected: Competitive Coding Contest"
    assert body["selection"]["title"] == "Competitive Coding Contest" and body["selection"]["resource_id"] == CODING
    mission = body["mission"]
    assert mission["mission_id"] == mission_id and mission["status"] == "needs_approval"
    assert mission["user_selection_required"] is False
    assert mission["selected_target"]["resource_id"] == CODING
    assert len(mission["pending_approvals"]) == 1

    card = _pending(api_client, mission_id)[0]
    assert card["target_source"] == "user_selection"
    assert card["target_source_label"] == "Selected by the student in Mission Workspace"
    assert card["parameters"] == {"student_id": "STU-DEMO-001", "event_id": CODING}
    assert card["precheck_status"] == "verified"

    timeline = api_client.get(f"/missions/{mission_id}/timeline", headers=STUDENT).json()["entries"]
    kinds = [(e["event_type"], e["status"]) for e in timeline]
    assert ("target_selected", "RUNNING") in kinds and ("approval_requested", "WAITING_FOR_APPROVAL") in kinds
    assert _registrations(session_factory, CODING) == 0  # selecting never writes

    # Approve + resume: exactly one registration.
    approval_id = card["approval_id"]
    assert api_client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers=ADMIN).status_code == 200
    resumed = api_client.post(f"/missions/{mission_id}/resume", headers=STUDENT).json()
    assert resumed["status"] == "completed" and resumed["user_selection_required"] is False
    assert _registrations(session_factory, CODING) == 1
    listing = api_client.get(f"/missions/{mission_id}/candidates", headers=STUDENT).json()
    assert listing["selection_open"] is False  # a completed action cannot be re-targeted
    assert _select(api_client, mission_id, PHOTO).status_code == 409


def test_invalid_candidates_are_rejected(api_client) -> None:
    mission_id = _discover(api_client)["mission_id"]

    assert _select(api_client, mission_id, 99999).status_code == 404
    bad_type = api_client.post(
        f"/missions/{mission_id}/selection", json={"resource_type": "room", "resource_id": CODING}, headers=STUDENT
    )
    assert bad_type.status_code == 422
    conflicting = _select(api_client, mission_id, HACKATHON)
    assert conflicting.status_code == 409 and "cannot be selected" in conflicting.json()["detail"]
    assert _pending(api_client, mission_id) == []


def test_stale_candidate_is_refused_and_its_status_updated(api_client, session_factory) -> None:
    mission_id = _discover(api_client)["mission_id"]
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == "STU-DEMO-001")).scalar_one()
        enrollment = session.execute(select(Enrollment).where(Enrollment.student_id == student.id)).scalars().first()
        event = session.get(Event, CODING)
        session.add(Exam(organization_id=student.organization_id, course_id=enrollment.course_id, exam_type="quiz",
                         scheduled_start=event.start_at, scheduled_end=event.end_at, location="Moved Exam"))
        session.commit()

    response = _select(api_client, mission_id, CODING)

    assert response.status_code == 409
    assert "changed after it was recommended. Please choose another event" in response.json()["detail"]
    candidate = next(
        c for c in api_client.get(f"/missions/{mission_id}/candidates", headers=STUDENT).json()["candidates"]
        if c["resource_id"] == CODING
    )
    assert candidate["assessment"]["status"] == "conflict" and candidate["selectable"] is False
    assert _pending(api_client, mission_id) == []


def test_refresh_rechecks_current_data_without_a_new_mission(api_client, session_factory) -> None:
    mission_id = _discover(api_client)["mission_id"]
    with session_factory() as session:
        event = session.get(Event, CODING)
        event.capacity = session.execute(
            select(func.count()).select_from(EventRegistration).where(EventRegistration.event_id == CODING)
        ).scalar_one()
        session.commit()

    listing = api_client.post(f"/missions/{mission_id}/candidates/refresh", headers=STUDENT).json()

    candidate = next(c for c in listing["candidates"] if c["resource_id"] == CODING)
    assert candidate["assessment"]["status"] == "full" and not candidate["selectable"]
    assert listing["mission_id"] == mission_id
    timeline = api_client.get(f"/missions/{mission_id}/timeline", headers=STUDENT).json()["entries"]
    assert "action_candidates_refreshed" in [e["event_type"] for e in timeline]


def test_repeated_selection_is_idempotent(api_client) -> None:
    mission_id = _discover(api_client)["mission_id"]
    assert _select(api_client, mission_id, CODING).json()["result"] == "selected"

    again = _select(api_client, mission_id, CODING)

    assert again.status_code == 200 and again.json()["result"] == "already_selected"
    assert len(_pending(api_client, mission_id)) == 1


def test_changing_selection_before_approval_replaces_the_request(api_client) -> None:
    mission_id = _discover(api_client)["mission_id"]
    first = _pending(api_client, _select(api_client, mission_id, CODING).json()["mission"]["mission_id"])[0]

    changed = _select(api_client, mission_id, PHOTO).json()

    assert changed["superseded_approval_id"] == first["approval_id"]
    pending = _pending(api_client, mission_id)
    assert len(pending) == 1 and pending[0]["parameters"]["event_id"] == PHOTO
    stale_decision = api_client.post(f"/approvals/{first['approval_id']}/decision", json={"decision": "approve"}, headers=ADMIN)
    assert stale_decision.status_code == 409


def test_only_the_missions_own_student_may_select(api_client) -> None:
    mission_id = _discover(api_client)["mission_id"]

    assert _select(api_client, mission_id, CODING, headers=OTHER_STUDENT).status_code == 403
    assert _select(api_client, mission_id, CODING, headers=ADMIN).status_code == 403  # an approver never selects
    assert _select(api_client, mission_id, CODING, headers={}).status_code == 401
    assert _pending(api_client, mission_id) == []


def test_mission_without_candidates_rejects_a_selection(api_client) -> None:
    named = api_client.post(
        "/missions",
        json={"goal": "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my "
                      "classes or exams, and register me for it."},
        headers=STUDENT,
    ).json()
    assert named["user_selection_required"] is False
    assert _select(api_client, named["mission_id"], CODING).status_code == 422


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest


@pytest.fixture()
def offline_transport(api_app):
    with TestClient(api_app) as client:
        api_client_module.set_default_http_client(client)
        yield client
    api_client_module.set_default_http_client(None)


def _workspace(mission_id: str):
    at = AppTest.from_file(APP_PATH)
    at.session_state["cn_current_mission_id"] = mission_id
    at.run(timeout=30)
    at.sidebar.radio[0].set_value("Mission Workspace").run(timeout=30)
    assert not at.exception
    return at


def test_workspace_shows_candidates_and_selecting_continues_the_mission(offline_transport) -> None:
    mission_id = offline_transport.post("/missions", json={"goal": GOAL}, headers=STUDENT).json()["mission_id"]

    at = _workspace(mission_id)

    body = "\n".join(md.value for md in at.markdown)
    assert "#### Choose an event" in body and "Competitive Coding Contest" in body
    select_buttons = [b for b in at.button if b.label == "Select this event"]
    unavailable = [b for b in at.button if b.label == "Unavailable"]
    assert len(select_buttons) == 2 and all(not b.disabled for b in select_buttons)
    assert unavailable and all(b.disabled for b in unavailable)
    assert any("CS301" in c.value for c in at.caption)  # why an unsafe event is unavailable

    select_buttons[0].click().run(timeout=60)
    assert not at.exception

    mission = offline_transport.get(f"/missions/{mission_id}", headers=STUDENT).json()
    assert mission["status"] == "needs_approval" and mission["selected_target"]["title"] == "Competitive Coding Contest"
    assert any("Selected: **Competitive Coding Contest**" in s.value for s in at.success)
    body = "\n".join(md.value for md in at.markdown)
    assert "**Selected** → **Pre-check VERIFIED** → **Approval required**" in body


def test_action_center_shows_the_student_selected_the_target(offline_transport) -> None:
    mission_id = offline_transport.post("/missions", json={"goal": GOAL}, headers=STUDENT).json()["mission_id"]
    _select(offline_transport, mission_id, CODING)

    at = AppTest.from_file(APP_PATH)
    at.run(timeout=30)
    at.sidebar.selectbox[0].set_value("admin-demo").run(timeout=30)
    at.sidebar.radio[0].set_value("Action Center").run(timeout=30)
    assert not at.exception
    assert any(c.value == "Target source: Selected by the student in Mission Workspace." for c in at.caption)


# ---------------------------------------------------------------------------
# S: real process restarts
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_env(tmp_path):
    db_path, chroma_path = tmp_path / "selection.db", tmp_path / "chroma"
    engine = create_db_engine(db_path=db_path)
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        run_seed(session)
    embedding = DeterministicHashEmbedding()
    store = PolicyVectorStore(path=chroma_path, collection_name="selection_test_policies", embedding_provider=embedding)
    ingest_policy_directory(get_rag_config().policy_dir, vector_store=store)
    env = {
        "CAMPUSNEXUS_DB_PATH": str(db_path),
        "CAMPUSNEXUS_VECTOR_STORE_PATH": str(chroma_path),
        "CAMPUSNEXUS_CHROMA_COLLECTION": "selection_test_policies",
        "CAMPUSNEXUS_EMBEDDING_PROVIDER": "deterministic",
        "CAMPUSNEXUS_LLM_PROVIDER": "mock",
    }
    return env, session_factory


def _helper(env: dict, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(HELPER), *args], env={**os.environ, **env}, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads([line for line in result.stdout.splitlines() if line.strip()][-1])


def test_candidates_and_selection_survive_real_process_restarts(isolated_env) -> None:
    env, session_factory = isolated_env

    discovered = _helper(env, "discover")  # process 1: plan, discover, record candidates, exit
    assert discovered["mission_status"] == "completed" and discovered["candidates"][str(CODING)] == "eligible"
    mission_id = discovered["mission_id"]

    selected = _helper(env, "select", "--mission-id", mission_id, "--event-id", str(CODING))  # process 2
    assert selected["candidates_seen"] == discovered["candidates"]  # read back from the database
    assert selected["result"] == "selected" and selected["mission_status"] == "needs_approval"

    done = _helper(env, "approve-and-resume", "--mission-id", mission_id)  # process 3
    assert done == {"mission_status": "completed", "selected_event_id": CODING}
    assert _registrations(session_factory, CODING) == 1
