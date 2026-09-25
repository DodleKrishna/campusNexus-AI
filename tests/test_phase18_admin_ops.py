"""Phase 18 -- admin console, HOD -> admin requests, escalation chain, audit, AI operations, secrets.

Offline (mock LLM); time pinned via ``create_app(clock=...)`` (Wednesday 10:20 IST:
CS303 is delayed, ECE's EC302 09:00-10:00 was not held).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.tenancy import ensure_default_organization
from app.api.main import create_app
from app.auth.accounts import seed_dev_accounts
from app.db.models.auth import AuthAccount
from app.db.models.communication import AccountNotification
from app.db.models.identity import Department, Student
from app.db.models.mission import AuditLog, Mission
from app.db.models.workflow import OperationAuditEvent, WorkflowRequest, WorkflowRequestStatus, WorkflowRequestType
from app.db.session import create_db_engine, create_session_factory
from app.llm.providers.mock import MockLLMProvider
from app.schemas.enums import MissionStatus, UserRole
from app.tools.build import build_default_tool_registry
from scripts.schedule_demo_class import demo_window

PASSWORD = "test-only-Passw0rd"
STUDENT, VERMA, HOD, ADMIN = "student@campusnexus.local", "faculty@campusnexus.local", "hod@campusnexus.local", "admin@campusnexus.local"
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 30, 10, 20, tzinfo=IST).astimezone(timezone.utc)
ADMIN_GETS = ("/admin/dashboard", "/admin/departments", "/admin/departments/CSE", "/admin/attendance", "/admin/complaints",
              "/admin/users", "/admin/ai-operations", "/admin/audit", "/admin/system", "/admin/notifications", "/admin/agents")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture()
def app(seeded_session, session_factory, knowledge_service, clock):
    with session_factory() as session:
        seed_dev_accounts(session, PASSWORD)
    return create_app(session_factory=session_factory, knowledge_service=knowledge_service, llm_provider=MockLLMProvider(),
                      tool_gateway=build_default_tool_registry(), clock=clock)


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def auth(client, email: str) -> dict:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def prepare(client, headers, message: str) -> dict:
    response = client.post("/requests/prepare", json={"message": message}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def ask(client, headers, key: str, message: str) -> dict:
    response = client.post(f"/admin/agents/{key}/query", json={"message": message}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Scope and authorization
# ---------------------------------------------------------------------------


def test_only_admins_reach_the_admin_console(client) -> None:
    for email in (STUDENT, VERMA, HOD):
        headers = auth(client, email)
        for path in ADMIN_GETS:
            assert client.get(path, headers=headers).status_code == 403, (email, path)
    assert client.get("/admin/dashboard").status_code == 401
    admin = auth(client, ADMIN)
    for path in ADMIN_GETS:
        assert client.get(path, headers=admin).status_code == 200, path


def test_admin_sees_every_department_and_filters_only_narrow(client, session_factory) -> None:
    admin = auth(client, ADMIN)
    rows = {r["code"]: r for r in client.get("/admin/departments", headers=admin).json()}
    assert set(rows) == {"CSE", "ECE", "MECH", "CIVIL"}
    assert rows["CSE"]["hod_name"] == "Dr. Kavita Iyer" and rows["ECE"]["hod_name"] == "Dr. Anjali Menon"
    with session_factory() as session:
        total = session.execute(select(func.count()).select_from(Student)).scalar_one()
    dashboard = client.get("/admin/dashboard", headers=admin).json()
    assert dashboard["total_students"] == total == sum(r["student_count"] for r in rows.values())
    assert dashboard["departments"] == 4 and dashboard["not_started_classes"] == 2  # CS303 delayed, EC302 not held
    cse = client.get("/admin/dashboard?department=CSE", headers=admin).json()
    assert cse["departments"] == 1 and all(a["class_label"].startswith("CSE") for a in cse["activity"])
    assert client.get("/admin/dashboard?department=XYZ", headers=admin).status_code == 404
    detail = client.get("/admin/departments/cse", headers=admin).json()
    assert detail["department"]["code"] == "CSE" and all(f["employee_code"].startswith("EMP-CSE") for f in detail["faculty"])


def test_institution_attendance_aggregates_every_department(client) -> None:
    admin, hod = auth(client, ADMIN), auth(client, HOD)
    data = client.get("/admin/attendance", headers=admin).json()
    by_code = {d["code"]: d for d in data["by_department"]}
    hod_risk = client.get("/hod/students", headers=hod).json()
    assert by_code["CSE"]["students"] == len(hod_risk)
    assert by_code["CSE"]["classes_attended"] == sum(r["classes_attended"] for r in hod_risk)
    assert by_code["CSE"]["students_below_threshold"] == sum(1 for r in hod_risk if r["courses_below_threshold"])
    assert {c["course_code"][:2] for c in data["insights"]["courses"]} >= {"CS", "EC", "ME", "CE"}
    assert data["low_attendance_students"] == len({e["student_id"] for e in data["insights"]["low_attendance"]})
    ranked = ask(client, admin, "academic", "Which departments have the most attendance-risk students?")["answer"]
    top = max(data["by_department"], key=lambda d: (d["students_below_threshold"], -ord(d["code"][0])))
    assert ranked.startswith(f"{top['code']} has the most attendance-risk students ({top['students_below_threshold']} below 75")


# ---------------------------------------------------------------------------
# HOD -> admin requests and the escalation chain
# ---------------------------------------------------------------------------


def test_hod_leave_goes_to_the_administration_and_is_decided_there(client, session_factory) -> None:
    hod, admin = auth(client, HOD), auth(client, ADMIN)
    draft = prepare(client, hod, "I need leave tomorrow.")["request"]
    assert (draft["request_type"], draft["reviewer_name"], draft["reviewer_role"], draft["routing_basis"]) == (
        "hod_leave", "the Administration", "admin", "administration")
    assert draft["routing_history"][0]["basis"] == "administration"
    assert client.get("/requests", headers=admin).json() == []  # not sent yet
    client.post("/requests", json={"request_id": draft["request_id"]}, headers=hod)

    inbox = client.get("/requests", headers=admin).json()
    assert [(r["request_id"], r["requester_role"], r["department_code"]) for r in inbox] == [(draft["request_id"], "hod", "CSE")]
    assert client.get("/admin/notifications", headers=admin).json()[0]["title"] == "New request for the administration"
    # Only the administration decides it: not the HOD (own request), not faculty.
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=hod).status_code == 403
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=auth(client, VERMA)).status_code == 403
    decided = client.post(f"/requests/{draft['request_id']}/approve", json={"comment": "Enjoy."}, headers=admin).json()
    assert decided["status"] == "approved" and decided["decided_by"] == "Priya Desai"
    assert client.post(f"/requests/{draft['request_id']}/reject", json={}, headers=admin).status_code == 409
    note = client.get("/faculty/notifications", headers=hod).json()[0]
    assert note["title"] == "Request approved" and "approved by the Administration (Priya Desai). Comment: Enjoy." in note["body"]
    assert client.get("/requests?box=mine", headers=hod).json()[0]["status"] == "approved"
    with session_factory() as session:
        audit = [e.event_type for e in session.execute(select(OperationAuditEvent).where(
            OperationAuditEvent.subject_id == draft["request_id"]).order_by(OperationAuditEvent.id)).scalars()]
    assert audit == ["request_prepared", "request_submitted", "request_approved"]


def test_hod_resource_request_needs_no_date(client) -> None:
    preview = prepare(client, auth(client, HOD), "I need a new projector for the CSE lab because the old one broke.")
    request = preview["request"]
    assert request["request_type"] == "department_resource" and request["context"]["affected_classes"] == []
    assert request["reviewer_role"] == "admin"


def test_admin_cannot_decide_requests_routed_to_faculty(client) -> None:
    student, admin = auth(client, STUDENT), auth(client, ADMIN)
    draft = prepare(client, student, "I need permission to attend the coding contest.")["request"]  # -> mentor
    client.post("/requests", json={"request_id": draft["request_id"]}, headers=student)
    response = client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=admin)
    assert response.status_code == 403 and response.json()["detail"] == "This request is not routed to the administration."


def test_escalation_chain_student_to_admin_and_legacy_rows(client, session_factory, clock) -> None:
    with session_factory() as session:
        aditi = session.execute(select(Student).where(Student.student_code == "STU-DEMO-001")).scalar_one()
        aditi.mentor_faculty_id = None
        aditi.department.hod_faculty_id = None
        verma = session.execute(select(AuthAccount).where(AuthAccount.email == VERMA)).scalar_one()
        session.add(WorkflowRequest(
            organization_id=aditi.organization_id, request_code="REQ-LEGACY02", request_type=WorkflowRequestType.FACULTY_LEAVE, status=WorkflowRequestStatus.NEEDS_REVIEW,
            requester_account_id=verma.id, requester_role="faculty", requester_faculty_id=verma.linked_faculty_id,
            department_id=aditi.department_id, title="Faculty Leave for Fri 02 Oct 2026", reason="Conference", context={},
            routing_basis="unresolved", routing_note="No reviewer.", created_at=clock.now, submitted_at=clock.now,
        ))
        session.commit()
    student, admin = auth(client, STUDENT), auth(client, ADMIN)
    draft = prepare(client, student, "I need permission to attend the coding contest.")["request"]
    assert (draft["routing_basis"], draft["reviewer_role"]) == ("admin_escalation", "admin")
    client.post("/requests", json={"request_id": draft["request_id"]}, headers=student)

    inbox = {r["request_id"]: r for r in client.get("/requests", headers=admin).json()}
    assert set(inbox) == {draft["request_id"], "REQ-LEGACY02"}
    legacy = inbox["REQ-LEGACY02"]
    assert (legacy["status"], legacy["routing_basis"]) == ("pending", "admin_escalation")
    assert legacy["routing_history"][-1]["basis"] == "admin_escalation"
    client.get("/requests", headers=admin)  # idempotent
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(OperationAuditEvent).where(
            OperationAuditEvent.event_type == "request_escalated", OperationAuditEvent.subject_id == "REQ-LEGACY02")).scalar_one() == 1
    assert client.get("/admin/dashboard", headers=admin).json()["escalations"] == 2
    assert client.get("/faculty/notifications", headers=auth(client, VERMA)).json()[0]["title"] == "Request escalated"


# ---------------------------------------------------------------------------
# Users, audit, AI operations, secrets
# ---------------------------------------------------------------------------


def test_user_management_is_safe_and_audited(client, session_factory) -> None:
    admin = auth(client, ADMIN)
    listed = client.get("/admin/users", headers=admin)
    assert "password" not in listed.text.lower() and "$2b$" not in listed.text
    users = {u["email"]: u for u in listed.json()}
    assert users[VERMA]["linked_faculty"] == "EMP-CSE-004 · Dr. Ashok Verma" and users[VERMA]["allowed_roles"] == ["faculty"]
    assert users[HOD]["allowed_roles"] == ["faculty", "hod"] and users[STUDENT]["allowed_roles"] == ["student"]

    assert client.post(f"/admin/users/{users[STUDENT]['account_id']}/role", json={"role": "faculty"}, headers=admin).status_code == 409
    assert client.post(f"/admin/users/{users[ADMIN]['account_id']}/active", json={"is_active": False}, headers=admin).status_code == 403
    off = client.post(f"/admin/users/{users[VERMA]['account_id']}/active", json={"is_active": False}, headers=admin).json()
    assert off["is_active"] is False
    assert client.post("/auth/login", json={"email": VERMA, "password": PASSWORD}).status_code == 401
    client.post(f"/admin/users/{users[VERMA]['account_id']}/active", json={"is_active": True}, headers=admin)
    reset = client.post(f"/admin/users/{users[VERMA]['account_id']}/reset-password", headers=admin).json()
    assert client.post("/auth/login", json={"email": VERMA, "password": reset["temporary_password"]}).status_code == 200
    with session_factory() as session:
        events = [e.event_type for e in session.execute(select(OperationAuditEvent).where(
            OperationAuditEvent.subject_type == "auth_account", OperationAuditEvent.event_type != "login").order_by(OperationAuditEvent.id)).scalars()]
        assert events == ["account_deactivated", "account_activated", "account_password_reset"]
        assert not any(reset["temporary_password"] in json.dumps(e.event_metadata) + e.message
                       for e in session.execute(select(OperationAuditEvent)).scalars())


def test_audit_log_merges_mission_and_operations_sources_read_only(client, session_factory, app) -> None:
    verma = auth(client, VERMA)
    session_id = client.get("/faculty/classes/today", headers=verma).json()[0]["session_id"]
    client.post(f"/faculty/classes/{session_id}/start", headers=verma)
    with session_factory() as session:
        an_hour_ago = NOW - timedelta(hours=1)
        organization_id = ensure_default_organization(session).id  # rows written outside the API name their organization
        session.add(Mission(organization_id=organization_id, mission_id="M-AUDIT-1", user_id="STU-DEMO-001", user_role=UserRole.STUDENT, original_goal="Register me",
                            status=MissionStatus.FAILED, created_at=an_hour_ago, updated_at=an_hour_ago))
        session.add(AuditLog(organization_id=organization_id, event_id="evt-1", mission_id="M-AUDIT-1", event_type="provider_unavailable", actor="mission_orchestrator",
                             message="Groq rate limit", timestamp=an_hour_ago,
                             event_metadata={"kind": "rate_limit", "status_code": 429, "provider": "groq"}))
        session.commit()
    admin = auth(client, ADMIN)
    entries = client.get("/admin/audit", headers=admin).json()
    actions = {e["action"] for e in entries}
    assert {"login", "class_started", "provider_unavailable"} <= actions
    assert {e["source"] for e in entries} == {"mission", "operations"}
    started = next(e for e in entries if e["action"] == "class_started")
    assert (started["actor"], started["role"], started["target"]) == ("Dr. Ashok Verma", "faculty", "attendance_session")
    assert all(e["source"] == "operations" for e in client.get("/admin/audit?source=operations", headers=admin).json())
    # No route can modify the audit trail.
    for route in app.routes:
        if getattr(route, "path", "").startswith("/admin/audit"):
            assert route.methods == {"GET"}

    ai = client.get("/admin/ai-operations", headers=admin).json()
    assert ai["failed_missions"] == 1 and ai["provider_errors"] == 1 and ai["rate_limit_incidents"] == 1
    assert ai["rag_ready"] and ai["database_ready"] and ai["provider"] == "mock" and ai["live"] is False
    assert ai["token_usage"] == "Not recorded by CampusNexus."
    assert client.get("/admin/dashboard", headers=admin).json()["system_errors_24h"] == 2  # the outage event + the failed mission
    failed = ask(client, admin, "enquiry", "Are there any failed agent workflows today?")["answer"]
    assert failed == "In the last 24 hours: 1 failed mission; audit events: 1 provider unavailable."


def test_no_secret_is_ever_exposed(client, monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_TOPSECRET_value")
    monkeypatch.setenv("CAMPUSNEXUS_JWT_SECRET", "jwt_TOPSECRET_value")
    admin = auth(client, ADMIN)
    system = client.get("/admin/system", headers=admin).json()
    assert system["settings"]["live_ai_key_configured"]["groq"] is True and system["settings"]["jwt_secret_configured"] is True
    llm = next(c for c in system["components"] if c["name"] == "LLM provider")
    assert llm["status"] == "degraded" and "not selected" in llm["detail"]  # mock mode is shown as degraded, never as live
    for path in ADMIN_GETS:
        body = client.get(path, headers=admin).text
        assert "TOPSECRET" not in body and "$2b$" not in body and "password_hash" not in body, path


def test_admin_enquiry_summarises_the_campus_from_real_data(client) -> None:
    admin = auth(client, ADMIN)
    dash = client.get("/admin/dashboard", headers=admin).json()
    answer = ask(client, admin, "enquiry", "Is the campus running normally today?")
    lines = answer["answer"].split("\n")
    assert lines[0] == "The campus needs attention today."
    assert lines[1] == (f"Classes: {dash['classes_today']} today — {dash['active_classes']} running, {dash['completed_classes']} completed, "
                        f"{dash['not_started_classes']} not started, {dash['cancelled_classes']} cancelled.")
    assert any(line.startswith(f"Complaints: {dash['sla_breaches']} open complaints past SLA") for line in lines)
    assert answer["facts"]["consulted_areas"] == ["operations", "attendance", "requests", "complaints", "system"]
    not_started = ask(client, admin, "academic", "How many classes have not started today?")["answer"]
    assert not_started.startswith("2 classes across the campus have not started today.")
    cse_only = ask(client, admin, "complaints", "Which complaints in CSE have breached SLA?")
    assert cse_only["facts"]["departments"] == ["CSE"]


def test_admin_state_persists_across_a_restart(client, knowledge_service, clock) -> None:
    hod, admin = auth(client, HOD), auth(client, ADMIN)
    draft = prepare(client, hod, "I need leave tomorrow.")["request"]
    client.post("/requests", json={"request_id": draft["request_id"]}, headers=hod)
    client.post(f"/requests/{draft['request_id']}/reject", json={"comment": "Board meeting."}, headers=admin)
    engine = create_db_engine(db_path=os.environ["CAMPUSNEXUS_DB_PATH"])
    try:
        fresh = create_app(session_factory=create_session_factory(engine), knowledge_service=knowledge_service,
                           llm_provider=MockLLMProvider(), tool_gateway=build_default_tool_registry(), clock=clock)
        with TestClient(fresh) as again:
            mine = again.get("/requests?box=mine", headers=auth(again, HOD)).json()[0]
            assert (mine["status"], mine["decision_reason"]) == ("rejected", "Board meeting.")
            assert any(e["action"] == "request_rejected" for e in again.get("/admin/audit", headers=auth(again, ADMIN)).json())
    finally:
        engine.dispose()


def test_demo_class_window_never_crosses_midnight() -> None:
    for hh, mm in ((23, 40), (23, 1), (23, 58)):
        start, end = demo_window(datetime(2026, 9, 24, hh, mm, tzinfo=IST), 60)
        assert start.date() == end.date() == datetime(2026, 9, 24).date() and end.strftime("%H:%M") == "23:59"
    start, end = demo_window(datetime(2026, 9, 24, 10, 7, tzinfo=IST), 60)
    assert (start.strftime("%H:%M"), end.strftime("%H:%M")) == ("10:05", "11:05")
