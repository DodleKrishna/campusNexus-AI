"""Phase 16 -- faculty operations, live attendance and student permission requests.

Offline: the mock LLM interprets messages; every status, count, timestamp and
reviewer comes from the database through deterministic code. Time is pinned
through ``create_app(clock=...)`` so class windows are exact.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.main import create_app
from app.auth.accounts import seed_dev_accounts
from app.db.models.academic import AttendanceRecord, Enrollment
from app.db.models.communication import Notification
from app.db.models.faculty import AttendanceSession, SessionAttendanceMark
from app.db.models.identity import Student
from app.db.models.mission import ApprovalRecord, Mission, ToolCallRecord
from app.db.models.workflow import OperationAuditEvent, WorkflowRequest
from app.db.session import create_db_engine, create_session_factory
from app.llm.providers.mock import MockLLMProvider
from app.rules import class_session as rules
from app.rules.request_routing import Reviewer, route_request
from app.schemas.workflow import PermissionIntent
from app.services.workflow_requests import resolve_event
from app.tools.build import build_default_tool_registry

PASSWORD = "test-only-Passw0rd"
STUDENT = "student@campusnexus.local"
VERMA = "faculty@campusnexus.local"  # teaches CS303 (Wed 10:00-11:00) to section 1; Aditi's mentor
SUNITA = "sunita.rao@campusnexus.local"  # teaches CS302 (Tue 10:00-11:00)
IST = timezone(timedelta(hours=5, minutes=30))


def ist(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 9, day, hh, mm, tzinfo=IST).astimezone(timezone.utc)


WED_1005 = ist(30, 10, 5)  # Wednesday: CS303 Computer Networks is on


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def clock() -> Clock:
    return Clock(WED_1005)


@pytest.fixture()
def app(seeded_session, session_factory, knowledge_service, clock):
    with session_factory() as session:
        seed_dev_accounts(session, PASSWORD)
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


def todays_class(client, headers, course_code: str) -> dict:
    classes = client.get("/faculty/classes/today", headers=headers).json()
    return next(c for c in classes if c["course_code"] == course_code)


def start_cn(client) -> tuple[dict, int]:
    verma = auth(client, VERMA)
    cn = todays_class(client, verma, "CS303")
    response = client.post(f"/faculty/classes/{cn['session_id']}/start", headers=verma)
    assert response.status_code == 200, response.text
    return verma, cn["session_id"]


# ---------------------------------------------------------------------------
# Deterministic rules
# ---------------------------------------------------------------------------


def test_start_window_boundaries_are_exact() -> None:
    start, end = ist(30, 10), ist(30, 11)
    opens = start - timedelta(minutes=rules.START_EARLY_MINUTES)
    assert rules.can_start("scheduled", start, end, opens).allowed
    assert not rules.can_start("scheduled", start, end, opens - timedelta(seconds=1)).allowed
    assert rules.can_start("scheduled", start, end, end - timedelta(seconds=1)).allowed
    assert not rules.can_start("scheduled", start, end, end).allowed
    assert not rules.can_start("active", start, end, start).allowed
    with pytest.raises(ValueError):
        rules.can_start("scheduled", start.replace(tzinfo=None), end, start)


def test_close_requires_every_student_marked_and_only_active() -> None:
    assert rules.can_close("active", 0).allowed
    assert rules.can_close("active", 1).reason == "1 student has not been marked yet."
    assert not rules.can_close("scheduled", 0).allowed
    assert not rules.can_mark("closed").allowed and rules.can_mark("active").allowed
    assert rules.can_cancel("scheduled").allowed and not rules.can_cancel("active").allowed
    assert [rules.counts_as_attended(m) for m in ("present", "late", "absent", "excused")] == [True, True, False, False]


def test_routing_rules() -> None:
    kavita, verma, sunita = Reviewer(1, "Dr. Kavita Iyer"), Reviewer(4, "Dr. Ashok Verma"), Reviewer(3, "Dr. Sunita Rao")
    one_class = route_request("event_permission", [sunita, sunita], verma)
    assert one_class.reviewer == sunita and one_class.basis == "affected_course_faculty"
    two_faculty = route_request("event_permission", [sunita, kavita], verma)
    assert two_faculty.reviewer == verma and two_faculty.basis == "mentor"
    no_class = route_request("od_request", [], verma)
    assert no_class.reviewer == verma and no_class.basis == "mentor"
    leave = route_request("leave_request", [sunita], verma)
    assert leave.reviewer == verma and leave.basis == "mentor"
    assert route_request("leave_request", [sunita], None).reviewer == sunita
    unresolved = route_request("event_permission", [sunita, kavita], None)
    assert unresolved.reviewer is None and unresolved.basis == "unresolved" and not unresolved.resolved
    with pytest.raises(ValueError):
        route_request("anything", [], verma)


def test_event_reference_must_appear_in_the_message(seeded_session) -> None:
    from app.services.workflow_requests import open_events

    events = open_events(seeded_session, datetime.now(timezone.utc))
    assert [e.title for e in resolve_event("permission to attend the coding contest", "coding contest", events)] == ["Competitive Coding Contest"]
    # A reference the student never wrote is ignored, never trusted.
    assert resolve_event("I need permission for an event", "Robotics Expo", events) == []


# ---------------------------------------------------------------------------
# Faculty scope and session lifecycle
# ---------------------------------------------------------------------------


def test_faculty_sees_only_own_classes_and_other_faculty_gets_403(client) -> None:
    verma = auth(client, VERMA)
    me = client.get("/faculty/me", headers=verma).json()
    assert me["full_name"] == "Dr. Ashok Verma" and [a["course_code"] for a in me["assignments"]] == ["CS303"]
    assert me["assignments"][0]["roster_size"] == 12

    today = client.get("/faculty/classes/today", headers=verma).json()
    assert [(c["course_code"], c["start_local"], c["status"]) for c in today] == [("CS303", "10:00", "scheduled")]
    assert today[0]["can_start"] is True and today[0]["tally"]["roster"] == 12

    sunita = auth(client, SUNITA)
    session_id = today[0]["session_id"]
    for path in (f"/faculty/classes/{session_id}", ):
        assert client.get(path, headers=sunita).status_code == 403
    for action in ("start", "close"):
        assert client.post(f"/faculty/classes/{session_id}/{action}", headers=sunita).status_code == 403
    denied = client.post(f"/faculty/classes/{session_id}/attendance", json={"marks": [{"student_id": "STU-DEMO-001", "status": "absent"}]}, headers=sunita)
    assert denied.status_code == 403
    # Students and unauthenticated callers never reach faculty endpoints.
    assert client.get("/faculty/classes/today", headers=auth(client, STUDENT)).status_code == 403
    assert client.get("/faculty/classes/today").status_code == 401


def test_session_lifecycle_start_mark_close_updates_counters_once(client, session_factory, clock) -> None:
    verma, session_id = start_cn(client)
    detail = client.get(f"/faculty/classes/{session_id}", headers=verma).json()
    assert detail["class_info"]["status"] == "active" and detail["class_info"]["actual_started_at"]
    assert len(detail["roster"]) == 12 and all(r["mark"] is None for r in detail["roster"])

    # Cannot start twice; cannot close with students unmarked.
    assert client.post(f"/faculty/classes/{session_id}/start", headers=verma).status_code == 409
    refused = client.post(f"/faculty/classes/{session_id}/close", headers=verma)
    assert refused.status_code == 409 and refused.json()["detail"] == "12 students have not been marked yet."

    client.post(f"/faculty/classes/{session_id}/attendance/all", json={"status": "present"}, headers=verma)
    clock.now = WED_1005 + timedelta(minutes=6)
    detail = client.post(
        f"/faculty/classes/{session_id}/attendance",
        json={"marks": [{"student_id": "STU2023023", "status": "absent"}, {"student_id": "STU2023024", "status": "late"}]},
        headers=verma,
    ).json()
    assert detail["class_info"]["tally"] == {"roster": 12, "present": 10, "absent": 1, "late": 1, "excused": 0, "unmarked": 0}
    outsider = client.post(f"/faculty/classes/{session_id}/attendance", json={"marks": [{"student_id": "STU2023009", "status": "present"}]}, headers=verma)
    assert outsider.status_code == 409 and "Not on this class's roster" in outsider.json()["detail"]

    with session_factory() as session:
        before = _counters(session, "STU2023023", "CS303"), _counters(session, "STU2023024", "CS303")
    closed = client.post(f"/faculty/classes/{session_id}/close", headers=verma)
    assert closed.status_code == 200 and closed.json()["class_info"]["status"] == "closed"
    assert client.post(f"/faculty/classes/{session_id}/close", headers=verma).status_code == 409
    assert client.post(f"/faculty/classes/{session_id}/attendance/all", json={"status": "absent"}, headers=verma).status_code == 409
    with session_factory() as session:
        absent_after, late_after = _counters(session, "STU2023023", "CS303"), _counters(session, "STU2023024", "CS303")
        assert absent_after == (before[0][0], before[0][1] + 1)  # conducted +1, attended unchanged
        assert late_after == (before[1][0] + 1, before[1][1] + 1)  # late counts as attended
        events = [e.event_type for e in session.execute(select(OperationAuditEvent).where(
            OperationAuditEvent.subject_type == "attendance_session").order_by(OperationAuditEvent.id)).scalars()]
        assert events == ["class_started", "attendance_marked", "attendance_marked", "class_closed"]
        student = session.execute(select(Student).where(Student.student_code == "STU2023023")).scalar_one()
        note = session.execute(select(Notification).where(Notification.student_id == student.id, Notification.category == "attendance")).scalar_one()
        assert "marked absent in Computer Networks" in note.body


def _counters(session, student_code: str, course_code: str) -> tuple[int, int]:
    from app.db.models.academic import Course

    row = session.execute(
        select(AttendanceRecord).join(Enrollment).join(Student, Student.id == Enrollment.student_id).join(Course, Course.id == Enrollment.course_id)
        .where(Student.student_code == student_code, Course.code == course_code)
    ).scalar_one()
    return row.classes_attended, row.classes_conducted


def test_class_cannot_start_outside_its_window(client, clock) -> None:
    verma = auth(client, VERMA)
    clock.now = ist(30, 9, 44)  # 16 minutes early
    cn = todays_class(client, verma, "CS303")
    assert cn["can_start"] is False and "15 minutes" in cn["start_blocked_reason"]
    assert client.post(f"/faculty/classes/{cn['session_id']}/start", headers=verma).status_code == 409
    clock.now = ist(30, 9, 45)
    assert client.post(f"/faculty/classes/{cn['session_id']}/start", headers=verma).status_code == 200


def test_attendance_is_unique_per_student_and_session(client, session_factory) -> None:
    verma, session_id = start_cn(client)
    for status in ("present", "absent", "present"):
        client.post(f"/faculty/classes/{session_id}/attendance", json={"marks": [{"student_id": "STU-DEMO-001", "status": status}]}, headers=verma)
    duplicate_in_request = client.post(
        f"/faculty/classes/{session_id}/attendance",
        json={"marks": [{"student_id": "STU-DEMO-001", "status": "present"}, {"student_id": "STU-DEMO-001", "status": "absent"}]},
        headers=verma,
    )
    assert duplicate_in_request.status_code == 422
    with session_factory() as session:
        marks = session.execute(select(SessionAttendanceMark).where(SessionAttendanceMark.session_id == session_id)).scalars().all()
        assert len(marks) == 1 and marks[0].status.value == "present"
        session.add(SessionAttendanceMark(session_id=session_id, student_id=marks[0].student_id, status=marks[0].status, marked_by_faculty_id=marks[0].marked_by_faculty_id))
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# Student live class status and the Enquiry Agent
# ---------------------------------------------------------------------------


def test_student_live_class_states(client, clock) -> None:
    student = auth(client, STUDENT)
    scheduled = client.get("/me/live-class", headers=student).json()
    assert scheduled["state"] == "scheduled" and scheduled["current"]["course_code"] == "CS303"
    assert scheduled["message"] == "Computer Networks is scheduled for 10:00 AM, but the faculty has not started the class session yet."

    clock.now = ist(30, 10, 4)
    verma, session_id = start_cn(client)
    clock.now = ist(30, 10, 11)
    client.post(f"/faculty/classes/{session_id}/attendance", json={"marks": [{"student_id": "STU-DEMO-001", "status": "present"}]}, headers=verma)
    live = client.get("/me/live-class", headers=student).json()
    assert live["state"] == "live" and live["my_attendance"] == "present"
    assert live["message"] == (
        "Yes. Computer Networks started at 10:04 AM. Attendance was opened by Dr. Ashok Verma, "
        "and your attendance was marked present at 10:11 AM."
    )

    clock.now = ist(30, 15, 0)
    assert client.get("/me/live-class", headers=student).json()["state"] == "live"  # still open: it is live
    client.post(f"/faculty/classes/{session_id}/attendance/all", json={"status": "present"}, headers=verma)
    client.post(f"/faculty/classes/{session_id}/close", headers=verma)
    idle = client.get("/me/live-class", headers=student).json()
    assert idle["state"] == "no_class" and idle["message"].startswith("You do not have a class scheduled right now.")


def test_enquiry_agent_answers_has_my_class_started_from_live_data(client, clock) -> None:
    student = auth(client, STUDENT)
    clock.now = ist(30, 10, 4)
    verma, session_id = start_cn(client)
    clock.now = ist(30, 10, 11)
    client.post(f"/faculty/classes/{session_id}/attendance", json={"marks": [{"student_id": "STU-DEMO-001", "status": "present"}]}, headers=verma)
    answer = client.post("/agents/enquiry/query", json={"message": "Has my class started?"}, headers=student).json()
    assert answer["answer"].split("\n")[0] == (
        "Yes. Computer Networks started at 10:04 AM. Attendance was opened by Dr. Ashok Verma, "
        "and your attendance was marked present at 10:11 AM."
    )
    assert answer["facts"]["live_class"]["state"] == "live"
    marked = client.post("/agents/enquiry/query", json={"message": "Was I marked present?"}, headers=student).json()
    assert "marked present at 10:11 AM" in marked["answer"]

    clock.now = datetime(2026, 10, 1, 10, 0, tzinfo=IST)  # Thursday 10:00 -- Software Engineering, not started
    nothing = client.post("/agents/enquiry/query", json={"message": "Has my class started?"}, headers=student).json()
    assert nothing["answer"].split("\n")[0] == "Software Engineering is scheduled for 10:00 AM, but the faculty has not started the class session yet."
    clock.now = datetime(2026, 10, 1, 9, 0, tzinfo=IST)
    assert client.post("/agents/enquiry/query", json={"message": "What class is next?"}, headers=student).json()["answer"] == (
        "You do not have a class scheduled right now. Your next class is Software Engineering at 10:00 AM in Block A - Room 207."
    )


# ---------------------------------------------------------------------------
# Faculty agents
# ---------------------------------------------------------------------------


def test_faculty_agents_answer_from_records_within_scope(client, clock) -> None:
    verma, session_id = start_cn(client)
    client.post(f"/faculty/classes/{session_id}/attendance/all", json={"status": "present"}, headers=verma)
    client.post(f"/faculty/classes/{session_id}/attendance", json={"marks": [{"student_id": "STU2023025", "status": "absent"}]}, headers=verma)

    def ask(key: str, message: str) -> dict:
        response = client.post(f"/faculty/agents/{key}/query", json={"message": message}, headers=verma)
        assert response.status_code == 200, response.text
        return response.json()

    assert ask("academic", "How many classes do I have today?")["answer"] == "You have 1 class today (Wednesday)."
    present = ask("enquiry", "How many students are present in CSE 3rd year Section 1?")
    assert present["answer"] == "In Computer Networks (CSE Year 3, Section 1), 10:00 AM: 11 present, 0 late, 1 absent, 0 excused (12 students)."
    assert ask("enquiry", "Who is absent?")["answer"].endswith("1 absent: Harsh Vardhan.")
    below = ask("academic", "Which students have attendance below 75%?")
    assert below["answer"].split("\n")[0] == "The attendance policy requires 75% per course."
    assert "Farhan Ali (66.0%)" in below["answer"] and "Harsh Vardhan (72.0%)" in below["answer"]
    assert below["evidence"] and below["evidence"][0]["document_id"] == "attendance-policy-v2"
    # DBMS is not Dr. Verma's class: structurally out of scope, no data returned.
    dbms = ask("academic", "Who is below 75% attendance in my DBMS class?")
    assert dbms["answer"] == "DBMS is not one of your assigned classes, so I can't show its records." and "table" not in dbms["facts"]
    assert ask("academic", "How many students are present in CSE 2nd year Section 1?")["answer"].startswith("You are not assigned")
    assert ask("permission", "Which student requests are pending?")["answer"] == "No student requests are waiting for your decision."
    assert client.post("/faculty/agents/action/query", json={"message": "x"}, headers=verma).status_code == 404


def test_sunita_sees_dbms_shortage_only_for_her_class(client) -> None:
    sunita = auth(client, SUNITA)
    answer = client.post("/faculty/agents/academic/query", json={"message": "Who is below 75% attendance in my DBMS class?"}, headers=sunita).json()
    assert "Database Management Systems (CSE Year 3, Section 1): Harsh Vardhan (68.0%)." in answer["answer"]
    assert "Computer Networks" not in answer["answer"]


# ---------------------------------------------------------------------------
# Permission requests
# ---------------------------------------------------------------------------


def prepare(client, headers, message: str) -> dict:
    response = client.post("/requests/prepare", json={"message": message}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_event_permission_preview_confirm_approve_and_notify(client, session_factory) -> None:
    student = auth(client, STUDENT)
    preview = prepare(client, student, "I need permission to attend the coding contest.")
    assert preview["outcome"] == "draft_ready"
    draft = preview["request"]
    assert draft["status"] == "draft" and draft["request_type"] == "event_permission"
    assert draft["context"]["event"]["title"] == "Competitive Coding Contest"
    assert draft["reviewer_name"] == "Dr. Ashok Verma" and draft["routing_basis"] == "mentor"
    assert draft["context"]["student_name"] == "Aditi Rao" and draft["context"]["student_section"] == "1"

    verma = auth(client, VERMA)
    # Not sent yet: the reviewer cannot see or decide a draft, and the student's list omits it.
    assert client.get("/requests", headers=verma).json() == []
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=verma).status_code == 404
    assert client.get("/requests", headers=student).json() == []

    sent = client.post("/requests", json={"request_id": draft["request_id"]}, headers=student)
    assert sent.status_code == 200 and sent.json()["status"] == "pending" and sent.json()["submitted_at"]
    assert client.post("/requests", json={"request_id": draft["request_id"]}, headers=student).status_code == 409

    inbox = client.get("/requests", headers=verma).json()
    assert [r["request_id"] for r in inbox] == [draft["request_id"]] and inbox[0]["student_name"] == "Aditi Rao"
    assert client.get("/faculty/dashboard", headers=verma).json()["pending_requests"] == 1

    # Another faculty member, and the student, cannot decide it.
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=auth(client, SUNITA)).status_code == 403
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=student).status_code == 403

    decided = client.post(f"/requests/{draft['request_id']}/approve", json={"comment": "All the best."}, headers=verma).json()
    assert decided["status"] == "approved" and decided["decided_by"] == "Dr. Ashok Verma" and decided["decision_reason"] == "All the best."
    assert client.post(f"/requests/{draft['request_id']}/reject", json={}, headers=verma).status_code == 409

    mine = client.get("/requests", headers=student).json()
    assert mine[0]["status"] == "approved"
    notes = client.get("/me/notifications", headers=student).json()
    assert notes[0]["body"] == (
        "Your Event Permission request for Competitive Coding Contest was approved by Dr. Ashok Verma. Comment: All the best."
    )
    assert any(n["title"] == "Request sent" for n in notes)
    with session_factory() as session:
        for model in (Mission, ApprovalRecord, ToolCallRecord):
            assert session.execute(select(func.count()).select_from(model)).scalar_one() == 0
        audit = [e.event_type for e in session.execute(select(OperationAuditEvent).where(OperationAuditEvent.subject_id == draft["request_id"]).order_by(OperationAuditEvent.id)).scalars()]
        assert audit == ["request_prepared", "request_submitted", "request_approved"]


def test_attendance_permission_routes_to_the_affected_course_faculty_and_can_be_rejected(client, clock) -> None:
    # Tuesday: Dr. Sunita Rao marks Aditi absent in DBMS.
    clock.now = ist(29, 10, 5)
    sunita = auth(client, SUNITA)
    dbms = todays_class(client, sunita, "CS302")
    client.post(f"/faculty/classes/{dbms['session_id']}/start", headers=sunita)
    client.post(f"/faculty/classes/{dbms['session_id']}/attendance/all", json={"status": "present"}, headers=sunita)
    client.post(f"/faculty/classes/{dbms['session_id']}/attendance", json={"marks": [{"student_id": "STU-DEMO-001", "status": "absent"}]}, headers=sunita)
    client.post(f"/faculty/classes/{dbms['session_id']}/close", headers=sunita)

    clock.now = WED_1005
    student = auth(client, STUDENT)
    draft = prepare(client, student, "I was absent yesterday because I was sick. I need attendance permission.")["request"]
    assert draft["request_type"] == "attendance_permission" and draft["reason"] == "I was sick"
    assert draft["reviewer_name"] == "Dr. Sunita Rao" and draft["routing_basis"] == "affected_course_faculty"
    affected = draft["context"]["affected_classes"]
    assert [(a["course_code"], a["my_mark"], a["session_status"]) for a in affected] == [("CS302", "absent", "closed")]

    client.post("/requests", json={"request_id": draft["request_id"], "reason": "I had a fever and a doctor's note."}, headers=student)
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=auth(client, VERMA)).status_code == 403
    rejected = client.post(f"/requests/{draft['request_id']}/reject", json={"comment": "Submit the medical certificate to the Academic Office."}, headers=sunita).json()
    assert rejected["status"] == "rejected" and rejected["reason"] == "I had a fever and a doctor's note."
    note = client.get("/me/notifications", headers=student).json()[0]
    assert note["title"] == "Request rejected" and "rejected by Dr. Sunita Rao" in note["body"]


def test_unresolvable_routing_needs_review_and_nobody_can_decide(client, session_factory) -> None:
    # Phase 17 escalates to the department HOD and Phase 18 to the administration, so remove the
    # mentor, the head AND every active admin account to leave nobody.
    from app.db.models.auth import AuthAccount
    from app.schemas.enums import UserRole

    with session_factory() as session:
        aditi = session.execute(select(Student).where(Student.student_code == "STU-DEMO-001")).scalar_one()
        aditi.mentor_faculty_id = None
        aditi.department.hod_faculty_id = None
        for admin in session.execute(select(AuthAccount).where(AuthAccount.role == UserRole.ADMIN)).scalars():
            admin.is_active = False
        session.commit()
    student = auth(client, STUDENT)
    draft = prepare(client, student, "I need permission to attend the coding contest.")["request"]
    assert draft["reviewer_name"] is None and draft["routing_basis"] == "unresolved"
    sent = client.post("/requests", json={"request_id": draft["request_id"]}, headers=student).json()
    assert sent["status"] == "needs_review"
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=auth(client, VERMA)).status_code == 403


def test_permission_agent_asks_instead_of_guessing(client) -> None:
    student = auth(client, STUDENT)
    unknown_event = prepare(client, student, "I need permission to attend the quantum physics contest.")
    assert unknown_event["outcome"] == "needs_clarification" and unknown_event["request"] is None
    unclear = prepare(client, student, "hello there")
    assert unclear["outcome"] == "not_supported"
    leave = prepare(client, student, "I need leave tomorrow afternoon.")
    assert leave["outcome"] == "draft_ready" and leave["request"]["context"]["day_part"] == "afternoon"
    assert leave["request"]["reviewer_name"] == "Dr. Ashok Verma"


def test_a_discarded_draft_is_never_listed_or_decidable(client) -> None:
    student = auth(client, STUDENT)
    draft = prepare(client, student, "I need leave tomorrow.")["request"]
    assert client.post(f"/requests/{draft['request_id']}/cancel", headers=student).json()["status"] == "cancelled"
    verma = auth(client, VERMA)
    assert client.get("/requests", headers=student).json() == [] and client.get("/requests", headers=verma).json() == []
    assert client.get(f"/requests/{draft['request_id']}", headers=verma).status_code == 404
    assert client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=verma).status_code == 404
    assert client.post("/requests", json={"request_id": draft["request_id"]}, headers=student).status_code == 409


def test_students_only_see_and_submit_their_own_requests(client, session_factory) -> None:
    from app.auth.passwords import hash_password
    from app.db.models.auth import AuthAccount
    from app.schemas.enums import UserRole

    with session_factory() as session:
        session.add(AuthAccount(email="rohan@campusnexus.local", password_hash=hash_password(PASSWORD), role=UserRole.STUDENT,
                                display_name="Rohan Mehta", linked_student_id="STU2023002"))
        session.commit()
    draft = prepare(client, auth(client, STUDENT), "I need leave tomorrow.")["request"]
    rohan = auth(client, "rohan@campusnexus.local")
    assert client.post("/requests", json={"request_id": draft["request_id"]}, headers=rohan).status_code == 404
    assert client.get(f"/requests/{draft['request_id']}", headers=rohan).status_code == 404
    # Phase 17: faculty may prepare their own requests; an account with no student/faculty link may not.
    assert client.post("/requests/prepare", json={"message": "leave"}, headers=auth(client, "admin@campusnexus.local")).status_code == 403


def test_state_persists_across_a_restart(client, knowledge_service, clock) -> None:
    verma, session_id = start_cn(client)
    client.post(f"/faculty/classes/{session_id}/attendance", json={"marks": [{"student_id": "STU-DEMO-001", "status": "present"}]}, headers=verma)
    student = auth(client, STUDENT)
    draft = prepare(client, student, "I need permission to attend the coding contest.")["request"]
    client.post("/requests", json={"request_id": draft["request_id"]}, headers=student)
    client.post(f"/requests/{draft['request_id']}/approve", json={}, headers=verma)

    # A brand-new engine, session factory and app over the same database file.
    engine = create_db_engine(db_path=os.environ["CAMPUSNEXUS_DB_PATH"])
    try:
        fresh = create_app(
            session_factory=create_session_factory(engine), knowledge_service=knowledge_service,
            llm_provider=MockLLMProvider(), tool_gateway=build_default_tool_registry(), clock=clock,
        )
        with TestClient(fresh) as again:
            headers = auth(again, STUDENT)
            live = again.get("/me/live-class", headers=headers).json()
            assert live["state"] == "live" and live["my_attendance"] == "present"
            assert again.get("/requests", headers=headers).json()[0]["status"] == "approved"
            assert again.get("/faculty/classes/today", headers=auth(again, VERMA)).json()[0]["status"] == "active"
    finally:
        engine.dispose()


def test_workflow_request_rows_are_not_action_approvals(session_factory, client) -> None:
    """The permission workflow never touches the Action Agent's approval tables."""
    student = auth(client, STUDENT)
    draft = prepare(client, student, "I need leave tomorrow.")["request"]
    client.post("/requests", json={"request_id": draft["request_id"]}, headers=student)
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(WorkflowRequest)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(ApprovalRecord)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(AttendanceSession)).scalar_one() == 0


def test_permission_intent_has_no_reviewer_or_status_field() -> None:
    # The LLM's structured output cannot carry a reviewer, a status or a date it computed.
    assert set(PermissionIntent.model_fields) == {"request_type", "event_reference", "date_reference", "specific_date", "day_part", "reason"}
