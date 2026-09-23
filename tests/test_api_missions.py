"""Tests for POST /missions, GET /missions/{id}[/timeline|/evidence], POST /missions/{id}/resume.

Covers spec §14 priorities: mission creation/retrieval, timeline
correctness, evidence rendering, ownership separation, and (combined with
test_api_approvals.py) the full approve->resume->duplicate-prevention path.
"""
from __future__ import annotations

DEMO_STUDENT = "STU-DEMO-001"
OTHER_STUDENT = "STU2023002"
REGISTER_GOAL = (
    "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
COMPLAINT_GOAL = "File a complaint: 'Hostel washroom tap still leaking after last repair.'"


def _create(api_client, goal: str, identity: str = "student-demo"):
    return api_client.post("/missions", json={"goal": goal}, headers={"X-Demo-Identity": identity})


def test_create_mission_missing_identity_is_401(api_client) -> None:
    response = api_client.post("/missions", json={"goal": COMPLAINT_GOAL})
    assert response.status_code == 401


def test_create_mission_rejects_goal_too_short(api_client) -> None:
    response = _create(api_client, "hi")
    assert response.status_code == 422


def test_create_mission_student_cannot_target_another_student(api_client) -> None:
    response = api_client.post(
        "/missions", json={"goal": COMPLAINT_GOAL, "student_id": OTHER_STUDENT}, headers={"X-Demo-Identity": "student-demo"}
    )
    assert response.status_code == 400


def test_create_and_retrieve_mission(api_client) -> None:
    created = _create(api_client, COMPLAINT_GOAL)
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "needs_approval"
    assert len(body["plan"]) == 1
    assert body["plan"][0]["agent"] == "action_agent"
    assert len(body["pending_approvals"]) == 1

    mission_id = body["mission_id"]
    fetched = api_client.get(f"/missions/{mission_id}", headers={"X-Demo-Identity": "student-demo"})
    assert fetched.status_code == 200
    assert fetched.json()["mission_id"] == mission_id
    assert fetched.json()["goal"] == COMPLAINT_GOAL


def test_get_unknown_mission_is_404(api_client) -> None:
    response = api_client.get("/missions/mission-does-not-exist", headers={"X-Demo-Identity": "student-demo"})
    assert response.status_code == 404


def test_student_cannot_view_another_students_mission(api_client) -> None:
    created = _create(api_client, COMPLAINT_GOAL)
    mission_id = created.json()["mission_id"]
    response = api_client.get(f"/missions/{mission_id}", headers={"X-Demo-Identity": "student-alt"})
    assert response.status_code == 403


def test_admin_can_view_any_students_mission(api_client) -> None:
    created = _create(api_client, COMPLAINT_GOAL)
    mission_id = created.json()["mission_id"]
    response = api_client.get(f"/missions/{mission_id}", headers={"X-Demo-Identity": "admin-demo"})
    assert response.status_code == 200


def test_mission_timeline_is_chronological_and_uses_real_events(api_client) -> None:
    created = _create(api_client, COMPLAINT_GOAL)
    mission_id = created.json()["mission_id"]

    response = api_client.get(f"/missions/{mission_id}/timeline", headers={"X-Demo-Identity": "student-demo"})
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert len(entries) >= 3
    event_types = [e["event_type"] for e in entries]
    assert "mission_created" in event_types
    assert "plan_generated" in event_types
    assert "approval_requested" in event_types
    timestamps = [e["timestamp"] for e in entries]
    assert timestamps == sorted(timestamps)
    assert all(e["status"] in {"PLANNING", "RUNNING", "VERIFYING", "WAITING_FOR_APPROVAL", "COMPLETED", "NEEDS_REVIEW", "FAILED"} for e in entries)


def test_mission_evidence_reflects_real_retrieved_citations(api_client) -> None:
    created = _create(api_client, REGISTER_GOAL)
    mission_id = created.json()["mission_id"]

    response = api_client.get(f"/missions/{mission_id}/evidence", headers={"X-Demo-Identity": "student-demo"})
    assert response.status_code == 200
    tasks = response.json()["tasks"]
    all_evidence = [ev for t in tasks for ev in t["evidence"]]
    assert len(all_evidence) > 0
    for ev in all_evidence:
        assert ev["document_id"]
        assert ev["source"]
        assert ev["snippet"]


def test_resume_requires_ownership(api_client) -> None:
    created = _create(api_client, COMPLAINT_GOAL)
    mission_id = created.json()["mission_id"]
    response = api_client.post(f"/missions/{mission_id}/resume", headers={"X-Demo-Identity": "student-alt"})
    assert response.status_code == 403
