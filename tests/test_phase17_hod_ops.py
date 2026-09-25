"""Phase 17 -- HOD operations, department scope, faculty -> HOD requests, escalation, old-schema upgrade.

Offline (mock LLM). Every count, state and reviewer is computed from the
seeded database; time is pinned through ``create_app(clock=...)``.
"""
from __future__ import annotations

import ast
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.db.tenancy import ensure_default_organization, sync_memberships
from app.api.main import create_app
from app.auth.accounts import seed_dev_accounts
from app.auth.passwords import hash_password
from app.db.models.auth import AuthAccount
from app.db.models.communication import StaffNotification
from app.db.models.faculty import FacultyProfile
from app.db.models.identity import Department, Student
from app.db.models.workflow import OperationAuditEvent, WorkflowRequest, WorkflowRequestStatus, WorkflowRequestType
from app.db.session import create_db_engine, create_session_factory, init_db, open_database
from app.llm.providers.mock import MockLLMProvider
from app.rules.class_session import ClassState, class_state
from app.schemas.enums import UserRole
from app.tools.build import build_default_tool_registry

PASSWORD = "test-only-Passw0rd"
STUDENT = "student@campusnexus.local"
VERMA = "faculty@campusnexus.local"  # CS303 Wed 10:00-11:00, CSE
SUNITA = "sunita.rao@campusnexus.local"
HOD = "hod@campusnexus.local"  # Dr. Kavita Iyer, head of CSE
ECE_HOD = "anjali.menon@campusnexus.local"  # created below; head of ECE by seed
IST = timezone(timedelta(hours=5, minutes=30))
REPO = Path(__file__).resolve().parents[1]


def ist(month: int, day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=IST).astimezone(timezone.utc)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def clock() -> Clock:
    return Clock(ist(9, 30, 10, 20))  # Wednesday 10:20 -- CS303 is 20 min past its start


@pytest.fixture()
def app(seeded_session, session_factory, knowledge_service, clock):
    with session_factory() as session:
        seed_dev_accounts(session, PASSWORD)
        anjali = session.execute(select(FacultyProfile).where(FacultyProfile.employee_code == "EMP-ECE-002")).scalar_one()
        session.add(AuthAccount(email=ECE_HOD, password_hash=hash_password(PASSWORD), role=UserRole.HOD,
                                display_name=anjali.full_name, linked_faculty_id=anjali.id, department_id=anjali.department_id))
        session.flush()
        sync_memberships(session, ensure_default_organization(session))  # Phase 22: authoritative membership
        session.commit()
    return create_app(
        session_factory=session_factory, knowledge_service=knowledge_service, llm_provider=MockLLMProvider(),
        tool_gateway=build_default_tool_registry(), clock=clock,
    )


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def auth(client, email: str) -> dict:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def ask(client, headers, key: str, message: str, base: str = "/hod/agents") -> dict:
    response = client.post(f"{base}/{key}/query", json={"message": message}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def prepare(client, headers, message: str) -> dict:
    response = client.post("/requests/prepare", json={"message": message}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Deterministic class state
# ---------------------------------------------------------------------------


def test_class_state_boundaries() -> None:
    start, end = ist(9, 30, 10), ist(9, 30, 11)
    assert class_state("scheduled", start, end, start - timedelta(seconds=1), 10) == ClassState.UPCOMING
    assert class_state("scheduled", start, end, start, 10) == ClassState.DUE
    assert class_state("scheduled", start, end, start + timedelta(minutes=10) - timedelta(seconds=1), 10) == ClassState.DUE
    assert class_state("scheduled", start, end, start + timedelta(minutes=10), 10) == ClassState.DELAYED
    assert class_state("scheduled", start, end, end, 10) == ClassState.NOT_HELD
    assert class_state("active", start, end, end + timedelta(hours=1), 10) == ClassState.ACTIVE
    assert class_state("closed", start, end, start, 10) == ClassState.COMPLETED
    assert class_state("cancelled", start, end, start + timedelta(minutes=30), 10) == ClassState.CANCELLED
    with pytest.raises(ValueError):
        class_state("scheduled", start, end, start, -1)


def test_grace_period_is_configurable(client, monkeypatch) -> None:
    hod = auth(client, HOD)
    assert client.get("/hod/activity", headers=hod).json()[0]["state"] == "delayed"  # 20 min > 10
    monkeypatch.setenv("CAMPUSNEXUS_CLASS_START_GRACE_MINUTES", "30")
    assert client.get("/hod/activity", headers=hod).json()[0]["state"] == "due"


# ---------------------------------------------------------------------------
# HOD scope and authorization
# ---------------------------------------------------------------------------


def test_hod_scope_is_resolved_from_the_department_record(client, session_factory) -> None:
    hod = auth(client, HOD)
    me = client.get("/hod/me", headers=hod).json()
    assert (me["full_name"], me["department_code"]) == ("Dr. Kavita Iyer", "CSE")
    dashboard = client.get("/hod/dashboard", headers=hod).json()
    with session_factory() as session:
        cse = session.execute(select(Department).where(Department.code == "CSE")).scalar_one()
        cse_students = session.execute(select(func.count()).select_from(Student).where(Student.department_id == cse.id)).scalar_one()
        cse_faculty = session.execute(select(func.count()).select_from(FacultyProfile).where(FacultyProfile.department_id == cse.id)).scalar_one()
    assert (dashboard["student_count"], dashboard["faculty_count"]) == (cse_students, cse_faculty)
    assert dashboard["classes_today"] == 1 and dashboard["not_started_classes"] == 1

    # The HOD role alone is not enough: the department record must name this faculty member.
    with session_factory() as session:
        cse = session.execute(select(Department).where(Department.code == "CSE")).scalar_one()
        cse.hod_faculty_id = None
        session.commit()
    assert client.get("/hod/me", headers=hod).status_code == 403


def test_hod_cannot_see_another_department(client) -> None:
    ece = auth(client, ECE_HOD)
    assert client.get("/hod/me", headers=ece).json()["department_code"] == "ECE"
    faculty = client.get("/hod/faculty", headers=ece).json()
    assert faculty and all(f["employee_code"].startswith("EMP-ECE") for f in faculty)
    students = client.get("/hod/students", headers=ece).json()
    names = {s["full_name"] for s in students}
    assert "Aditi Rao" not in names and "Farhan Ali" not in names and "Divya Menon" in names
    assert all(c["course_code"].startswith("EC") for c in client.get("/hod/attendance", headers=ece).json()["courses"])
    below = ask(client, ece, "academic", "Which students are below 75% attendance?")
    assert "Farhan" not in below["answer"] and "Harsh" not in below["answer"]
    # No parameter can widen the scope: the department is never read from the request.
    assert client.get("/hod/students?department=CSE", headers=ece).json() == students


def test_students_and_faculty_cannot_reach_hod_data(client) -> None:
    for email in (STUDENT, VERMA):
        headers = auth(client, email)
        for path in ("/hod/me", "/hod/dashboard", "/hod/students", "/hod/faculty", "/hod/attendance", "/hod/complaints"):
            assert client.get(path, headers=headers).status_code == 403, (email, path)
        assert client.post("/hod/agents/enquiry/query", json={"message": "overview"}, headers=headers).status_code == 403
    assert client.get("/faculty/classes/today", headers=auth(client, STUDENT)).status_code == 403


# ---------------------------------------------------------------------------
# Faculty -> HOD requests
# ---------------------------------------------------------------------------


def test_faculty_leave_goes_to_the_hod_with_affected_classes(client, session_factory) -> None:
    verma, hod = auth(client, VERMA), auth(client, HOD)
    preview = prepare(client, verma, "I need leave on 2026-10-07 because of a conference.")
    draft = preview["request"]
    assert preview["outcome"] == "draft_ready" and draft["request_type"] == "faculty_leave" and draft["status"] == "draft"
    assert (draft["reviewer_name"], draft["routing_basis"], draft["requester_kind"]) == ("Dr. Kavita Iyer", "department_hod", "faculty")
    assert draft["reason"] == "a conference"
    affected = draft["context"]["affected_classes"]
    assert [(a["course_code"], a["class_label"], a["start_local"], a["roster_size"], a["substitute"]) for a in affected] == [
        ("CS303", "CSE 3-1", "10:00", 12, "Not assigned")
    ]
    assert draft["context"]["faculty_name"] == "Dr. Ashok Verma"

    # Drafts belong to the faculty member only: listed under "mine", invisible to the HOD.
    assert [r["request_id"] for r in client.get("/requests?box=mine", headers=verma).json()] == [draft["request_id"]]
    assert client.get("/requests", headers=hod).json() == []
    sent = client.post("/requests", json={"request_id": draft["request_id"]}, headers=verma).json()
    assert sent["status"] == "pending"
    inbox = client.get("/requests", headers=hod).json()
    assert [r["request_id"] for r in inbox] == [draft["request_id"]] and inbox[0]["requester_name"] == "Dr. Ashok Verma"
    assert client.get("/hod/dashboard", headers=hod).json()["pending_faculty_requests"] == 1

    # Nobody decides their own request; other faculty cannot decide it; students cannot either.
    own = client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=verma)
    assert own.status_code == 403 and own.json()["detail"] == "You cannot decide your own request."
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=auth(client, SUNITA)).status_code == 403
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=auth(client, STUDENT)).status_code == 403
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=auth(client, ECE_HOD)).status_code == 403

    decided = client.post(f"/requests/{draft['request_id']}/approve", json={"comment": "Arrange notes for the class."}, headers=hod).json()
    assert decided["status"] == "approved" and decided["decided_by"] == "Dr. Kavita Iyer"
    notes = client.get("/faculty/notifications", headers=verma).json()
    assert notes[0]["body"] == "Your Faculty Leave request for 7 October was approved by the HOD, CSE. Comment: Arrange notes for the class."
    hod_notes = client.get("/faculty/notifications", headers=hod).json()
    assert any(n["title"] == "New faculty request" and "Dr. Ashok Verma" in n["body"] for n in hod_notes)
    with session_factory() as session:
        audit = [e.event_type for e in session.execute(
            select(OperationAuditEvent).where(OperationAuditEvent.subject_id == draft["request_id"]).order_by(OperationAuditEvent.id)).scalars()]
    assert audit == ["request_prepared", "request_submitted", "request_approved"]


def test_hod_own_request_goes_to_the_administration_and_faculty_chat_routes_requests(client) -> None:
    # Phase 18 closed the gap: a department head's own request is routed to the administration.
    hod = auth(client, HOD)
    draft = prepare(client, hod, "I need leave tomorrow.")["request"]
    assert (draft["reviewer_name"], draft["routing_basis"], draft["request_type"]) == ("the Administration", "administration", "hod_leave")
    assert client.post("/requests", json={"request_id": draft["request_id"]}, headers=hod).json()["status"] == "pending"
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=hod).status_code == 403

    reply = ask(client, auth(client, VERMA), "enquiry", "I need leave tomorrow.", base="/faculty/agents")
    assert reply["action_hint"]["agent_key"] == "permission" and reply["facts"]["route"] == "permission_request"
    assert ask(client, auth(client, VERMA), "permission", "What requests are waiting for me?", base="/faculty/agents")["answer"] == (
        "No student requests are waiting for your decision."
    )


def test_class_substitution_without_classes_asks_instead_of_guessing(client) -> None:
    preview = prepare(client, auth(client, VERMA), "I need a substitute tomorrow.")  # Thursday: no CS303
    assert preview["outcome"] == "needs_clarification" and preview["request"] is None


# ---------------------------------------------------------------------------
# Escalation of student requests
# ---------------------------------------------------------------------------


def test_unresolvable_student_request_escalates_to_the_department_hod(client, session_factory) -> None:
    with session_factory() as session:
        aditi = session.execute(select(Student).where(Student.student_code == "STU-DEMO-001")).scalar_one()
        aditi.mentor_faculty_id = None
        session.commit()
    student, hod = auth(client, STUDENT), auth(client, HOD)
    draft = prepare(client, student, "I need permission to attend the coding contest.")["request"]
    assert (draft["reviewer_name"], draft["routing_basis"]) == ("Dr. Kavita Iyer", "hod_escalation")
    client.post("/requests", json={"request_id": draft["request_id"]}, headers=student)
    assert client.get("/hod/dashboard", headers=hod).json()["escalated_student_requests"] == 1
    # The ECE head never sees it.
    assert client.get("/requests", headers=auth(client, ECE_HOD)).json() == []


def test_existing_needs_review_requests_are_escalated_once(client, session_factory, clock) -> None:
    with session_factory() as session:
        account = session.execute(select(AuthAccount).where(AuthAccount.email == STUDENT)).scalar_one()
        aditi = session.execute(select(Student).where(Student.student_code == "STU-DEMO-001")).scalar_one()
        session.add(WorkflowRequest(
            organization_id=aditi.organization_id, request_code="REQ-LEGACY01", request_type=WorkflowRequestType.LEAVE_REQUEST, status=WorkflowRequestStatus.NEEDS_REVIEW,
            requester_account_id=account.id, requester_role="student", student_id="STU-DEMO-001", department_id=aditi.department_id,
            title="Leave Request for Fri 02 Oct 2026", reason="Family function", context={}, routing_basis="unresolved",
            routing_note="No reviewer could be determined.", created_at=clock.now, submitted_at=clock.now,
        ))
        session.commit()
    hod = auth(client, HOD)
    inbox = client.get("/requests", headers=hod).json()
    assert [(r["request_id"], r["status"], r["routing_basis"]) for r in inbox] == [("REQ-LEGACY01", "pending", "hod_escalation")]
    client.get("/requests", headers=hod)
    with session_factory() as session:
        events = session.execute(select(func.count()).select_from(OperationAuditEvent).where(
            OperationAuditEvent.event_type == "request_escalated")).scalar_one()
        assert events == 1
    note = client.get("/me/notifications", headers=auth(client, STUDENT)).json()[0]
    assert note["title"] == "Request escalated" and "HOD, CSE" in note["body"]


# ---------------------------------------------------------------------------
# Department monitoring and analytics
# ---------------------------------------------------------------------------


def test_classes_not_started_and_delayed_warning(client, session_factory, clock) -> None:
    hod = auth(client, HOD)
    answer = ask(client, hod, "enquiry", "Which classes haven't started?")
    assert answer["answer"].split("\n")[0] == "1 CSE class hasn't started today."
    assert "Computer Networks (CSE 3-1, 10:00–11:00, Dr. Ashok Verma)" in answer["answer"]
    client.get("/hod/dashboard", headers=hod)
    client.get("/hod/dashboard", headers=hod)
    with session_factory() as session:
        warnings = session.execute(select(func.count()).select_from(StaffNotification).where(
            StaffNotification.category == "delayed_class")).scalar_one()
    assert warnings == 1  # one per class meeting, however often the dashboard is viewed

    verma = auth(client, VERMA)
    session_id = client.get("/faculty/classes/today", headers=verma).json()[0]["session_id"]
    assert client.post(f"/faculty/classes/{session_id}/start", headers=verma).status_code == 200
    assert ask(client, hod, "academic", "Which classes haven't started?")["answer"].startswith("Every CSE class due so far today has started")
    running = ask(client, hod, "academic", "How many classes are running now?")
    assert running["answer"].startswith("1 CSE class is running now: Computer Networks")

    client.post(f"/faculty/classes/{session_id}/attendance/all", json={"status": "present"}, headers=verma)
    client.post(f"/faculty/classes/{session_id}/attendance", json={"marks": [{"student_id": "STU2023025", "status": "absent"}]}, headers=verma)
    present = ask(client, hod, "academic", "How many students were present in CSE 3rd year Section 1 today?")
    assert present["answer"].startswith("CSE 3-1 today: 11 present out of 12 marked across 1 session")

    clock.now = ist(9, 30, 18)  # the other CSE Wednesday classes: none; CS303 is still open (live)
    assert client.get("/hod/activity", headers=hod).json()[0]["state"] == "active"


def test_not_held_when_the_scheduled_time_passes(client, clock) -> None:
    clock.now = ist(9, 30, 11, 30)
    hod = auth(client, HOD)
    assert client.get("/hod/activity", headers=hod).json()[0]["state"] == "not_held"
    assert "Scheduled time has passed without the class being started" in ask(client, hod, "academic", "Which classes did not start on time?")["answer"]


def test_student_risk_and_attendance_insights_are_computed_from_counters(client) -> None:
    hod = auth(client, HOD)
    risk = {r["student_id"]: r for r in client.get("/hod/students", headers=hod).json()}
    farhan = risk["STU2023023"]  # CS301 35/50, CS302 44/50, CS303 33/50, CS304 44/50
    assert farhan["courses_below_threshold"] == ["CS301", "CS303"] and farhan["overall_percentage"] == 78.0
    assert (farhan["classes_attended"], farhan["classes_conducted"]) == (156, 200)
    aditi = risk["STU-DEMO-001"]  # 34+47+44+40 / 200
    assert aditi["courses_below_threshold"] == ["CS301"] and aditi["overall_percentage"] == 82.5
    assert all(r["student_id"] != "STU2023009" for r in risk.values())  # ECE student

    insights = client.get("/hod/attendance", headers=hod).json()
    courses = {c["course_code"]: c for c in insights["courses"]}
    lowest = min((c for c in courses.values() if c["percentage"] is not None), key=lambda c: c["percentage"])
    answer = ask(client, hod, "academic", "Which course has the lowest attendance?")["answer"]
    assert answer.startswith(f"{lowest['course_title']} ({lowest['class_label']}, {lowest['faculty_name']}) has the lowest attendance: {lowest['percentage']}%")
    below = ask(client, hod, "enquiry", "Which students are below 75% attendance?")
    assert below["answer"].startswith("The attendance policy requires 75")
    assert "Farhan Ali — CS301 70.0%" in below["answer"] and below["evidence"][0]["document_id"] == "attendance-policy-v2"
    assert {e["student_id"] for e in insights["low_attendance"]} >= {"STU2023023", "STU2023025", "STU-DEMO-001"}


def test_department_overview_enquiry_is_scoped_and_read_only(client, session_factory) -> None:
    hod = auth(client, HOD)
    with session_factory() as session:
        before = session.execute(select(func.count()).select_from(WorkflowRequest)).scalar_one()
    overview = ask(client, hod, "enquiry", "Is everything running normally in CSE today?")
    lines = overview["answer"].split("\n")
    assert lines[0] == "CSE needs attention today." and lines[1].startswith("Classes: 1 today — 0 running, 0 completed, 1 not started.")
    assert overview["facts"]["consulted_areas"] == ["operations", "attendance", "requests", "complaints"]
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(WorkflowRequest)).scalar_one() == before
    complaints = ask(client, hod, "complaints", "Which complaints in CSE have breached SLA?")
    assert complaints["verification_status"] == "verified"
    assert ask(client, hod, "events", "Which classes haven't started?")["answer"] == "That is a question for the Academic Agent."


def test_hod_state_persists_across_a_restart(client, knowledge_service, clock) -> None:
    verma, hod = auth(client, VERMA), auth(client, HOD)
    draft = prepare(client, verma, "I need leave on 2026-10-07 because of a conference.")["request"]
    client.post("/requests", json={"request_id": draft["request_id"]}, headers=verma)
    client.post(f"/requests/{draft['request_id']}/reject", json={"comment": "Exam week."}, headers=hod)

    engine = create_db_engine(db_path=os.environ["CAMPUSNEXUS_DB_PATH"])
    try:
        fresh = create_app(session_factory=create_session_factory(engine), knowledge_service=knowledge_service,
                           llm_provider=MockLLMProvider(), tool_gateway=build_default_tool_registry(), clock=clock)
        with TestClient(fresh) as again:
            mine = again.get("/requests?box=mine", headers=auth(again, VERMA)).json()
            assert mine[0]["status"] == "rejected" and mine[0]["decision_reason"] == "Exam week."
            assert again.get("/faculty/notifications", headers=auth(again, VERMA)).json()[0]["title"] == "Request rejected"
            assert again.get("/hod/me", headers=auth(again, HOD)).json()["department_code"] == "CSE"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Old databases
# ---------------------------------------------------------------------------


def _make_pre_phase16_db(path: Path) -> None:
    """A database as Phase 15 left it: no section/mentor/HOD columns, no Phase 16/17 tables."""
    engine = create_db_engine(db_path=str(path))
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in ("staff_notifications", "operation_audit_events", "workflow_requests", "session_attendance_marks",
                      "attendance_sessions", "teaching_assignments"):
            conn.execute(text(f'DROP TABLE "{table}"'))
        conn.execute(text(
            "CREATE TABLE students_old AS SELECT id, student_code, user_id, department_id, year, semester, cgpa, interests, "
            "career_goal, created_at FROM students"))
        conn.execute(text("DROP TABLE students"))
        conn.execute(text("ALTER TABLE students_old RENAME TO students"))
        conn.execute(text("CREATE TABLE departments_old AS SELECT id, code, name, created_at FROM departments"))
        conn.execute(text("DROP TABLE departments"))
        conn.execute(text("ALTER TABLE departments_old RENAME TO departments"))
        conn.execute(text("DROP TABLE faculty_profiles"))
        conn.execute(text("INSERT INTO departments (id, code, name, created_at) VALUES (1, 'CSE', 'CS', '2025-01-01 00:00:00')"))
    engine.dispose()


def test_open_database_upgrades_an_older_schema(tmp_path) -> None:
    path = tmp_path / "phase15.db"
    _make_pre_phase16_db(path)
    old = create_db_engine(db_path=str(path))
    with pytest.raises(OperationalError):
        with create_session_factory(old)() as session:
            session.execute(select(Department)).scalars().all()  # hod_faculty_id does not exist yet
    old.dispose()

    engine = open_database(str(path))
    try:
        with create_session_factory(engine)() as session:
            assert [d.code for d in session.execute(select(Department)).scalars()] == ["CSE"]
            assert session.execute(select(func.count()).select_from(Student)).scalar_one() == 0
            assert session.execute(select(func.count()).select_from(StaffNotification)).scalar_one() == 0
    finally:
        engine.dispose()
    again = open_database(str(path))  # idempotent
    again.dispose()


def _entry_points() -> list[Path]:
    return sorted([*REPO.glob("eval/*.py"), *REPO.glob("scripts/*.py")])


@pytest.mark.parametrize("path", _entry_points(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_entry_point_upgrades_before_use(path: Path) -> None:
    """An entry point that opens a database must upgrade it (open_database, init_db or upgrade_schema)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = {node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
             for node in ast.walk(tree) if isinstance(node, ast.Call)}
    if calls & {"create_db_engine", "create_database_engine"}:
        assert calls & {"init_db", "upgrade_schema", "open_database"}, f"{path.name} opens a database without upgrading it"
