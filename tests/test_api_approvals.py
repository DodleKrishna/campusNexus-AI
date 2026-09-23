"""Tests for GET /approvals/pending, POST /approvals/{id}/decision.

Covers spec §14 priorities: approval authorization (student rejected, admin
allowed), the full approve->resume path, and duplicate-decision prevention.
"""
from __future__ import annotations

from app.db.repositories.events import get_registration

REGISTER_GOAL = (
    "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
REJECTED_GOAL = "File a complaint: 'Library projector bulb needs replacement.'"


def _create(api_client, goal: str):
    return api_client.post("/missions", json={"goal": goal}, headers={"X-Demo-Identity": "student-demo"})


def test_pending_approvals_scoped_to_own_student(api_client) -> None:
    _create(api_client, REGISTER_GOAL)

    own = api_client.get("/approvals/pending", headers={"X-Demo-Identity": "student-demo"})
    assert own.status_code == 200
    assert len(own.json()) == 1

    other = api_client.get("/approvals/pending", headers={"X-Demo-Identity": "student-alt"})
    assert other.status_code == 200
    assert other.json() == []

    as_admin = api_client.get("/approvals/pending", headers={"X-Demo-Identity": "admin-demo"})
    assert as_admin.status_code == 200
    assert len(as_admin.json()) == 1


def test_pending_approval_is_fully_enriched(api_client) -> None:
    _create(api_client, REGISTER_GOAL)
    approval = api_client.get("/approvals/pending", headers={"X-Demo-Identity": "admin-demo"}).json()[0]

    assert approval["tool_name"] == "register_event"
    assert approval["target_resource"]
    assert approval["student_id"] == "STU-DEMO-001"
    assert "event_id" in approval["parameters"]
    assert isinstance(approval["evidence"], list)


def test_student_cannot_decide_an_approval(api_client) -> None:
    _create(api_client, REGISTER_GOAL)
    approval_id = api_client.get("/approvals/pending", headers={"X-Demo-Identity": "admin-demo"}).json()[0]["approval_id"]

    response = api_client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers={"X-Demo-Identity": "student-demo"}
    )
    assert response.status_code == 403


def test_faculty_cannot_decide_an_approval(api_client) -> None:
    _create(api_client, REGISTER_GOAL)
    approval_id = api_client.get("/approvals/pending", headers={"X-Demo-Identity": "admin-demo"}).json()[0]["approval_id"]

    response = api_client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers={"X-Demo-Identity": "faculty-demo"}
    )
    assert response.status_code == 403


def test_decide_unknown_approval_is_404(api_client) -> None:
    response = api_client.post(
        "/approvals/appr-does-not-exist/decision", json={"decision": "approve"}, headers={"X-Demo-Identity": "admin-demo"}
    )
    assert response.status_code == 404


def test_full_approve_and_resume_persists_real_row(api_client, session_factory) -> None:
    created = _create(api_client, REGISTER_GOAL)
    mission_id = created.json()["mission_id"]
    approval_id = api_client.get("/approvals/pending", headers={"X-Demo-Identity": "admin-demo"}).json()[0]["approval_id"]

    decision = api_client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers={"X-Demo-Identity": "admin-demo"}
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "approved"

    resumed = api_client.post(f"/missions/{mission_id}/resume", headers={"X-Demo-Identity": "student-demo"})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"

    with session_factory() as session:
        registration = get_registration(session, 10, "STU-DEMO-001")  # event 10 = Competitive Coding Contest
        assert registration is not None
        assert registration.status.value == "confirmed"


def test_duplicate_decision_is_rejected_not_silently_reapplied(api_client) -> None:
    _create(api_client, REGISTER_GOAL)
    approval_id = api_client.get("/approvals/pending", headers={"X-Demo-Identity": "admin-demo"}).json()[0]["approval_id"]

    first = api_client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers={"X-Demo-Identity": "admin-demo"}
    )
    assert first.status_code == 200

    second = api_client.post(
        f"/approvals/{approval_id}/decision", json={"decision": "reject"}, headers={"X-Demo-Identity": "admin-demo"}
    )
    assert second.status_code == 409


def test_rejected_action_never_executes(api_client, session_factory) -> None:
    created = _create(api_client, REJECTED_GOAL)
    mission_id = created.json()["mission_id"]
    approval_id = api_client.get("/approvals/pending", headers={"X-Demo-Identity": "admin-demo"}).json()[0]["approval_id"]

    decision = api_client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "reject", "reason": "duplicate ticket"},
        headers={"X-Demo-Identity": "admin-demo"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "rejected"

    resumed = api_client.post(f"/missions/{mission_id}/resume", headers={"X-Demo-Identity": "student-demo"})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "failed"
