"""Phase 22C (demo scope) -- the primary demo paths are tenant-safe.

Organization A is the seeded demo institution; B ("northfield-demo") reuses A's
department code CSE and employee code EMP-CSE-004 (fixtures from
test_phase22b_tenant_isolation). Each demo path is exercised from both sides:
A keeps working, B never sees or changes A's rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import AttendanceSession, OperationAuditEvent, TeachingAssignment, WorkflowRequest
from app.db.models.faculty import AttendanceSessionStatus
from app.db.models.mission import ApprovalRecord, Mission
from tests.test_phase10_mission_flow import REGISTER_GOAL
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_ADMIN, A_FACULTY, A_STUDENT, B_ADMIN, B_FACULTY, B_STUDENT, app, bearer, client, login, orgs,
)

HOD = "hod@campusnexus.local"


def headers(client, email: str) -> dict:
    return bearer(login(client, email)["access_token"])


def test_every_role_dashboard_serves_its_own_organization(client) -> None:
    for email, path in ((A_STUDENT, "/me/dashboard"), (A_FACULTY, "/faculty/dashboard"), (HOD, "/hod/dashboard"),
                        (A_ADMIN, "/admin/dashboard"), (B_STUDENT, "/me/dashboard"), (B_FACULTY, "/faculty/dashboard"),
                        (B_ADMIN, "/admin/dashboard")):
        response = client.get(path, headers=headers(client, email))
        assert response.status_code == 200, (email, path, response.text)
    a_admin = client.get("/admin/departments", headers=headers(client, A_ADMIN)).json()
    b_admin = client.get("/admin/departments", headers=headers(client, B_ADMIN)).json()
    assert len(a_admin) > 1 and [d["code"] for d in b_admin] == ["CSE"]
    assert "Northfield" in str(client.get("/admin/departments/CSE", headers=headers(client, B_ADMIN)).json())
    assert "Northfield" not in str(client.get("/admin/departments/CSE", headers=headers(client, A_ADMIN)).json())


def test_admins_manage_only_their_organizations_accounts(client, session_factory) -> None:
    b_users = {u["email"] for u in client.get("/admin/users", headers=headers(client, B_ADMIN)).json()}
    assert b_users == {B_STUDENT, B_FACULTY, B_ADMIN}
    a_users = {u["email"] for u in client.get("/admin/users", headers=headers(client, A_ADMIN)).json()}
    assert A_STUDENT in a_users and not a_users & b_users
    a_student_id = client.get("/auth/me", headers=headers(client, A_STUDENT)).json()["id"]
    refused = client.post(f"/admin/users/{a_student_id}/active", json={"is_active": False}, headers=headers(client, B_ADMIN))
    assert refused.status_code == 404
    assert client.get("/auth/me", headers=headers(client, A_STUDENT)).status_code == 200  # untouched


def test_requests_and_notifications_stay_in_the_organization(client, orgs, session_factory) -> None:
    student = headers(client, A_STUDENT)
    draft = client.post("/requests/prepare", json={"message": "I need permission to attend the coding contest."},
                        headers=student).json()["request"]
    assert client.post("/requests", json={"request_id": draft["request_id"]}, headers=student).status_code == 200
    assert [r["request_id"] for r in client.get("/requests", headers=headers(client, A_FACULTY)).json()] == [draft["request_id"]]
    assert client.get("/requests", headers=headers(client, B_FACULTY)).json() == []
    assert client.get("/requests", headers=headers(client, B_ADMIN)).json() == []
    assert client.get(f"/requests/{draft['request_id']}", headers=headers(client, B_ADMIN)).status_code in (403, 404)
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=headers(client, B_ADMIN)).status_code in (403, 404)
    decided = client.post(f"/requests/{draft['request_id']}/approve", json={"comment": "ok"}, headers=headers(client, A_FACULTY))
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    assert any("approved" in n["body"] for n in client.get("/me/notifications", headers=student).json())
    assert client.get("/me/notifications", headers=headers(client, B_STUDENT)).json() == []
    with session_factory() as s:  # every row the workflow wrote belongs to A
        request = s.execute(select(WorkflowRequest).where(WorkflowRequest.request_code == draft["request_id"])).scalar_one()
        assert request.organization_id == orgs["a"]
        events = s.execute(select(OperationAuditEvent).where(OperationAuditEvent.subject_id == draft["request_id"])).scalars().all()
        assert events and {e.organization_id for e in events} == {orgs["a"]}


def test_audit_shows_only_the_organizations_events(client) -> None:
    headers(client, A_STUDENT), headers(client, A_FACULTY)  # A sign-ins are audited in A
    b_actors = {e["actor"] for e in client.get("/admin/audit", headers=headers(client, B_ADMIN)).json()}
    assert b_actors and b_actors <= {"Priya North", "Nina North", "Dr. Omar North", "system", None}
    a_actions = {e["action"] for e in client.get("/admin/audit", headers=headers(client, A_ADMIN)).json()}
    assert "login" in a_actions


def test_attendance_of_another_organization_is_not_reachable(client, orgs, session_factory) -> None:
    with session_factory() as s:  # an A class meeting
        assignment = s.execute(select(TeachingAssignment).where(TeachingAssignment.organization_id == orgs["a"])).scalars().first()
        start = datetime.now(timezone.utc) + timedelta(minutes=5)
        meeting = AttendanceSession(organization_id=orgs["a"], teaching_assignment_id=assignment.id, course_id=assignment.course_id,
                                    faculty_id=assignment.faculty_id, session_date=start.date(), scheduled_start=start,
                                    scheduled_end=start + timedelta(hours=1), room="A-101", status=AttendanceSessionStatus.SCHEDULED)
        s.add(meeting)
        s.commit()
        meeting_id = meeting.id
    b_faculty = headers(client, B_FACULTY)
    # Another organization's class answers exactly like a class that does not exist (no existence oracle).
    missing = client.get("/faculty/classes/999999", headers=b_faculty)
    foreign = client.get(f"/faculty/classes/{meeting_id}", headers=b_faculty)
    assert (foreign.status_code, foreign.json()) == (missing.status_code, missing.json()) and foreign.status_code in (403, 404)
    assert client.post(f"/faculty/classes/{meeting_id}/start", headers=b_faculty).status_code in (403, 404)
    with session_factory() as s:
        assert s.get(AttendanceSession, meeting_id).status == AttendanceSessionStatus.SCHEDULED  # not started by B
    assert client.get("/faculty/attendance", headers=b_faculty).json() == []
    assert client.get("/faculty/attendance", headers=headers(client, A_FACULTY)).json() != []


def test_missions_and_approvals_are_written_in_the_callers_organization(client, orgs, session_factory) -> None:
    mission = client.post("/missions", json={"goal": REGISTER_GOAL}, headers={"X-Demo-Identity": "student-demo"}).json()
    pending = client.get("/approvals/pending", headers={"X-Demo-Identity": "admin-demo"}).json()
    assert any(a["mission_id"] == mission["mission_id"] for a in pending)
    with session_factory() as s:
        assert s.get(Mission, mission["mission_id"]).organization_id == orgs["a"]
        approvals = s.execute(select(ApprovalRecord).where(ApprovalRecord.mission_id == mission["mission_id"])).scalars().all()
        assert approvals and {a.organization_id for a in approvals} == {orgs["a"]}
