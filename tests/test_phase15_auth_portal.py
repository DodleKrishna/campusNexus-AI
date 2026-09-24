"""Phase 15 -- JWT authentication, role authorization, the student portal API,
agent chat identity scoping and the read-only Enquiry Agent.

Offline: the mock LLM plans enquiries and phrases answers; every number comes
from the seeded database through the existing deterministic rules.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agents.action.agent import ActionAgent
from app.agents.enquiry.agent import EnquiryAgent
from app.api.main import create_app
from app.auth.accounts import DEV_ACCOUNTS, resolve_seed_password, seed_dev_accounts
from app.auth.tokens import issue_token
from app.db.models.auth import AuthAccount
from app.db.models.mission import ApprovalRecord, Mission, ToolCallRecord
from app.llm.base import LLMRateLimitError
from app.llm.providers.mock import MockLLMProvider
from app.rules.attendance import compute_attendance
from app.rules.attendance_standing import AttendanceStanding, SlotStatus, attendance_standing, slot_status
from app.schemas.agent_chat import SpecialistAnswer
from app.schemas.enums import UserRole
from app.services import student_portal
from app.tools.build import build_default_tool_registry

PASSWORD = "test-only-Passw0rd"
STUDENT_EMAIL = "student@campusnexus.local"


@pytest.fixture()
def accounts(seeded_session, session_factory):
    with session_factory() as session:
        seed_dev_accounts(session, PASSWORD)
        # A second student account, to prove one student never sees another's data.
        from app.auth.passwords import hash_password

        session.add(AuthAccount(
            email="rohan@campusnexus.local", password_hash=hash_password(PASSWORD), role=UserRole.STUDENT,
            display_name="Rohan Mehta", linked_student_id="STU2023002",
        ))
        session.commit()


@pytest.fixture()
def client(api_app, accounts):
    with TestClient(api_app) as c:
        yield c


def _login(client, email=STUDENT_EMAIL, password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def _auth(client, email=STUDENT_EMAIL) -> dict:
    response = _login(client, email)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_login_returns_a_token_and_the_role_home(client) -> None:
    body = _login(client).json()

    assert body["token_type"] == "bearer" and body["access_token"]
    assert body["user"] == {
        "id": body["user"]["id"], "email": STUDENT_EMAIL, "role": "student", "display_name": "Aditi Rao",
        "student_id": "STU-DEMO-001", "department_code": "CSE", "department_name": body["user"]["department_name"],
        "home_route": "/student",
    }
    assert "password" not in str(body).lower()
    for spec, home in zip(DEV_ACCOUNTS, ("/student", "/faculty", "/hod", "/admin")):
        assert _login(client, spec.email).json()["user"]["home_route"] == home


def test_wrong_password_and_unknown_email_are_rejected_the_same_way(client) -> None:
    wrong = _login(client, password="nope")
    unknown = _login(client, email="nobody@campusnexus.local")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"] == "Incorrect email or password."


def test_passwords_are_stored_only_as_bcrypt_hashes(session_factory, accounts) -> None:
    with session_factory() as session:
        stored = session.execute(select(AuthAccount.password_hash)).scalars().all()
    assert stored and all(h.startswith("$2") and PASSWORD not in h for h in stored)


def test_me_requires_a_valid_unexpired_token(client) -> None:
    headers = _auth(client)
    me = client.get("/auth/me", headers=headers).json()
    assert me["email"] == STUDENT_EMAIL and "password_hash" not in me

    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
    account_id = me["id"]
    expired, _ = issue_token(account_id, "student", now=datetime.now(timezone.utc) - timedelta(hours=2), lifetime=timedelta(minutes=5))
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401 and response.json()["detail"] == "Your session expired. Please sign in again."
    assert client.post("/auth/logout", headers=headers).status_code == 204


def test_inactive_account_cannot_use_its_token(client, session_factory) -> None:
    headers = _auth(client)
    with session_factory() as session:
        account = session.execute(select(AuthAccount).where(AuthAccount.email == STUDENT_EMAIL)).scalar_one()
        account.is_active = False
        session.commit()
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert _login(client).status_code == 401


def test_seeding_is_idempotent_and_the_dev_password_is_not_hardcoded(session_factory, accounts, tmp_path, monkeypatch) -> None:
    with session_factory() as session:
        assert seed_dev_accounts(session, "another-password") == []  # existing accounts untouched
    monkeypatch.delenv("CAMPUSNEXUS_DEMO_PASSWORD", raising=False)
    file = tmp_path / "dev_credentials.txt"
    first, source = resolve_seed_password(file)
    assert source == str(file) and len(first) >= 12 and resolve_seed_password(file)[0] == first
    monkeypatch.setenv("CAMPUSNEXUS_DEMO_PASSWORD", "from-env")
    assert resolve_seed_password(file) == ("from-env", "$CAMPUSNEXUS_DEMO_PASSWORD")


# ---------------------------------------------------------------------------
# Authorization + student data scope
# ---------------------------------------------------------------------------


def test_student_endpoints_are_student_only(client) -> None:
    for email in ("faculty@campusnexus.local", "hod@campusnexus.local", "admin@campusnexus.local"):
        headers = _auth(client, email)
        assert client.get("/me/dashboard", headers=headers).status_code == 403
        assert client.post("/agents/academic/query", json={"message": "attendance"}, headers=headers).status_code == 403
    assert client.get("/me/dashboard").status_code == 401


def test_each_student_only_ever_sees_their_own_data(client) -> None:
    aditi = client.get("/me/profile", headers=_auth(client)).json()
    rohan = client.get("/me/profile", headers=_auth(client, "rohan@campusnexus.local")).json()

    assert aditi["student_code"] == "STU-DEMO-001" and rohan["student_code"] == "STU2023002"
    # There is no way to name another student: query parameters are not accepted as identity.
    other = client.get("/me/attendance?student_id=STU2023002", headers=_auth(client)).json()
    mine = client.get("/me/attendance", headers=_auth(client)).json()
    assert other == mine


# ---------------------------------------------------------------------------
# Student portal
# ---------------------------------------------------------------------------


def test_dashboard_uses_real_records_and_deterministic_rules(client) -> None:
    body = client.get("/me/dashboard", headers=_auth(client)).json()

    assert body["profile"]["full_name"] == "Aditi Rao" and body["profile"]["first_name"] == "Aditi"
    assert body["profile"]["year"] == 3 and body["profile"]["semester"] == 5
    assert body["cgpa"] == 7.8
    assert body["overall_attendance"] == {"percentage": 82.5, "classes_attended": 165, "classes_conducted": 200}
    assert body["courses_below_requirement"] == 1
    assert body["next_exam"]["course_code"] == "CS301" and body["next_exam"]["eligibility"] == "not_eligible"
    assert body["pending_requests"] == 0


def test_attendance_standing_and_recovery_come_from_the_rules(client) -> None:
    courses = {c["course_code"]: c for c in client.get("/me/attendance", headers=_auth(client)).json()}

    os_course = courses["CS301"]
    assert os_course["current_percentage"] == 68.0 and os_course["required_percentage"] == 75.0
    assert os_course["standing"] == "below_requirement" and os_course["classes_needed_to_reach_threshold"] == 14
    assert os_course["policy"]["title"] and os_course["policy"]["version"] == "v2"
    assert courses["CS302"]["standing"] == "good"
    detail = client.get("/me/attendance/cs301", headers=_auth(client)).json()
    assert detail == os_course
    assert client.get("/me/attendance/ME201", headers=_auth(client)).status_code == 404


def test_standing_and_slot_status_boundaries() -> None:
    assert attendance_standing(compute_attendance(30, 40, Decimal("75"))) == AttendanceStanding.AT_RISK  # exactly 75%, can miss 0
    assert attendance_standing(compute_attendance(39, 50, Decimal("75"))) == AttendanceStanding.AT_RISK  # can miss 2
    assert attendance_standing(compute_attendance(40, 50, Decimal("75"))) == AttendanceStanding.GOOD  # can miss 3
    assert attendance_standing(compute_attendance(37, 50, Decimal("75"))) == AttendanceStanding.BELOW_REQUIREMENT  # 74%
    assert attendance_standing(compute_attendance(0, 0, Decimal("75"))) == AttendanceStanding.UNKNOWN
    assert slot_status("10:00", "11:00", time(9, 59)) == SlotStatus.UPCOMING
    assert slot_status("10:00", "11:00", time(10, 0)) == SlotStatus.NOW
    assert slot_status("10:00", "11:00", time(11, 0)) == SlotStatus.COMPLETED


def test_today_schedule_marks_the_current_class(session_factory, seeded_session) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    monday_1030 = datetime(2026, 9, 28, 10, 30, tzinfo=ist)  # a Monday: CS301 10:00-11:00
    with session_factory() as session:
        schedule = student_portal.today_schedule(session, "STU-DEMO-001", now=monday_1030)

    assert schedule.weekday == "Monday"
    assert [(s.course_code, s.status) for s in schedule.slots] == [("CS301", "now")]
    assert schedule.slots[0].instructor == "Dr. Manoj Pillai" and schedule.slots[0].location == "Block A - Room 204"


def test_exams_notifications_and_requests_are_real_rows(client) -> None:
    headers = _auth(client)
    exams = client.get("/me/exams", headers=headers).json()
    assert [e["course_code"] for e in exams] == ["CS301", "CS302", "CS303", "CS304"]
    assert exams[0]["venue"] == "Exam Hall 1" and exams[0]["eligibility_caveat"]
    notifications = client.get("/me/notifications", headers=headers).json()
    assert notifications and all(n["title"] and n["category"] for n in notifications)
    assert client.get("/me/requests", headers=headers).json() == []
    assert client.get("/me/timetable/today", headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# Agent chat
# ---------------------------------------------------------------------------


def test_catalog_lists_ui_names_over_backend_agents(client) -> None:
    body = client.get("/agents", headers=_auth(client)).json()
    agents = {a["key"]: a for a in body["agents"]}
    assert agents["placements"]["display_name"] == "Placement Agent" and agents["placements"]["backend_agent"] == "career_agent"
    assert agents["complaints"]["backend_agent"] == "campus_services_agent"
    # Phase 16: the Permission Agent is live (prepare -> preview -> Confirm & Send).
    assert agents["permission"]["available"] is True and body["live_ai"] is False


def test_agent_query_is_scoped_to_the_signed_in_student(client) -> None:
    body = {"message": "What is my OS attendance?", "student_id": "STU2023002"}  # ignored

    answer = client.post("/agents/academic/query", json=body, headers=_auth(client)).json()

    assert answer["verification_status"] == "verified"
    assert answer["facts"]["attendance"]["classes_attended"] == 34 and answer["facts"]["attendance"]["classes_conducted"] == 50
    assert answer["evidence"] and answer["evidence"][0]["title"].startswith("Attendance Policy")


@pytest.mark.parametrize("agent,message,fact", [
    ("events", "Will the cloud workshop clash with my schedule?", "assessments"),
    ("placements", "What internships am I eligible for?", "eligibilities"),
    ("complaints", "Are any of my complaints overdue?", "case_assessments"),
])
def test_specialist_agents_answer_through_chat(client, agent, message, fact) -> None:
    answer = client.post(f"/agents/{agent}/query", json={"message": message}, headers=_auth(client)).json()
    assert answer["verification_status"] == "verified" and answer["facts"][fact]


def test_events_chat_checks_clashes_against_the_students_schedule(client) -> None:
    answer = client.post(
        "/agents/events/query", json={"message": "Will the cloud workshop clash with my schedule?"}, headers=_auth(client)
    ).json()
    cloud = next(a for a in answer["facts"]["assessments"] if "Cloud" in a["event"]["title"])
    assert cloud["conflict_check_performed"] and [c["course_code"] for c in cloud["timetable_conflicts"]] == ["CS302"]


def test_unknown_or_unavailable_agents_are_refused(client) -> None:
    headers = _auth(client)
    for key in ("permission", "action", "action_agent", "nope"):
        assert client.post(f"/agents/{key}/query", json={"message": "hi"}, headers=headers).status_code == 404


def test_enquiry_consults_specialists_and_answers_from_verified_facts(client) -> None:
    answer = client.post(
        "/agents/enquiry/query", json={"message": "Do I have anything important today?"}, headers=_auth(client)
    ).json()

    assert [c["agent_key"] for c in answer["consulted"]] == ["academic", "academic", "events", "placements", "complaints"]
    assert answer["verification_status"] == "verified" and answer["notices"] == []
    text = answer["answer"]
    assert "Classes today" in text and "Next exam: Operating Systems midterm" in text
    assert "Complaints: 3 case(s); past SLA: CASE-0001, CASE-0002, CASE-0003." in text


def test_enquiry_is_read_only_and_points_actions_to_their_workflow(client, session_factory, monkeypatch) -> None:
    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the Enquiry Agent reached the Action Agent")

    monkeypatch.setattr(ActionAgent, "handle", forbidden)
    answer = client.post(
        "/agents/enquiry/query", json={"message": "Register me for the hackathon"}, headers=_auth(client)
    ).json()

    assert answer["action_hint"]["agent_key"] == "events"
    assert "can't make changes" in answer["action_hint"]["message"]
    with session_factory() as session:
        for model in (Mission, ApprovalRecord, ToolCallRecord):
            assert session.execute(select(func.count()).select_from(model)).scalar_one() == 0


def test_enquiry_leaves_out_unverified_results_and_is_honest_about_class_status() -> None:
    def consult(agent_key, objective):
        if agent_key == "academic":
            return SpecialistAnswer(
                agent_key="academic", agent="academic_agent", objective=objective, verification_status="verified",
                facts={"timetable": [{"course_code": "CS303", "course_title": "Computer Networks", "weekday": 2,
                                      "start_time": "10:00", "end_time": "11:00", "location": "Block A - Room 206"}]},
            )
        return SpecialistAnswer(agent_key=agent_key, agent="x", objective=objective, verification_status="failed", answer="INVENTED")

    wednesday_1015 = datetime(2026, 9, 30, 4, 45, tzinfo=timezone.utc)  # 10:15 IST
    agent = EnquiryAgent(llm_provider=MockLLMProvider(), consult=consult)
    live = agent.handle("Has my class started?", now=wednesday_1015)
    assert live.answer == (
        "Your timetable shows Computer Networks scheduled now (10:00–11:00, Block A - Room 206), "
        "but live class-start status is not currently available."
    )
    broad = agent.handle("Anything important today?", now=wednesday_1015)
    assert "INVENTED" not in broad.answer and broad.verification_status == "needs_review"
    assert "Complaints Agent's result could not be verified, so it was left out." in broad.notices


def test_provider_outage_in_chat_is_a_clean_503(seeded_session, session_factory, knowledge_service, accounts) -> None:
    class Down(MockLLMProvider):
        def classify_academic_intent(self, *args, **kwargs):
            raise LLMRateLimitError("429", provider="groq", model="m", status_code=429)

    app = create_app(session_factory=session_factory, knowledge_service=knowledge_service, llm_provider=Down(),
                     tool_gateway=build_default_tool_registry())
    with TestClient(app) as client:
        response = client.post("/agents/academic/query", json={"message": "attendance"}, headers=_auth(client))
    assert response.status_code == 503
    assert response.json()["detail"] == "Live AI is temporarily unavailable. Please try again shortly."
