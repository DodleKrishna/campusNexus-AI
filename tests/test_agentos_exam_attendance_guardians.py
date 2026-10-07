"""AgentOS V2 Phase 4: the Exam Guardian, the Attendance Guardian, the shared follow-up policy and the worker edge.

Offline and deterministic: the clock is pinned (``app.state.clock``), the brain is the offline mock policy or a
``ScriptedAgentBrain``, and nothing sleeps or calls a provider.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agentos.attendance_guardian import GUARDIAN_AGENT_KEY as ATTENDANCE_KEY, scan_attendance
from app.agentos.bootstrap import build_agent_runtime
from app.agentos.brain import ScriptedAgentBrain
from app.agentos.events import wake_mission
from app.agentos.exam_guardian import GUARDIAN_AGENT_KEY as EXAM_KEY
from app.agentos.providers import MockAgentBrain
from app.agentos.schemas import AgentDecision, DecisionKind
from app.agentos.worker import OWNER_INACTIVE
from app.db.models import (
    AgentMission, AgentMissionStatus, AgentStepStatus, AttendanceFollowup, AttendanceIntervention, AttendanceSession,
    AttendanceSessionStatus, AuthAccount, DomainEvent, Enrollment, Exam, ExamAttendance, ExamFollowup, ExamStatus,
    ExamTarget, FollowupStatus, InterventionStatus, OperationAuditEvent, OrganizationMembership, Student,
    TeachingAssignment, User, WorkflowRequest, WorkflowRequestStatus, WorkflowRequestType,
)
from app.db.models.exam import ExamAttendanceStatus
from app.db.tenant_session import TenantSessionFactory
from app.rules import attendance_intervention as att_rules
from app.rules import exam_rules
from app.rules.attendance_intervention import AttendancePolicy
from app.rules.exam_rules import ExamPolicy
from app.rules.followup_policy import FollowupPolicy, check_contact_limits
from app.schemas.enums import MembershipStatus, UserRole
from app.services import attendance_monitor
from app.services import exams as exam_service
from app.services.class_schedule import local, roster
from tests.test_agentos_assignment_guardian import (  # noqa: F401 -- fixtures and helpers
    POLICY, SMALL, T0, Clock, audit_types, auth, clock, email, mission_of, rows, run_worker, small_class,
)
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_ADMIN, A_FACULTY, A_STUDENT, B_ADMIN, B_FACULTY, B_STUDENT, PASSWORD, app, bearer, client, login, orgs,
)

NO_QUIET = dict(quiet_start=time(0, 0), quiet_end=time(0, 0))
EXAM_POLICY = ExamPolicy(
    reminder=FollowupPolicy(cooldown=timedelta(minutes=20), max_attempts=3, **NO_QUIET),
    absence=FollowupPolicy(cooldown=timedelta(hours=6), max_attempts=2, **NO_QUIET),
    reminder_hours=(24.0, 2.0, 0.5), start_grace=timedelta(minutes=15), terminal_after_end=timedelta(days=2))
ATT_POLICY = AttendancePolicy(contact=FollowupPolicy(cooldown=timedelta(hours=6), max_attempts=2, **NO_QUIET),
                              grace=timedelta(minutes=15), resolution_window=timedelta(hours=24))
START = T0 + timedelta(hours=30)  # the exam; reminder checkpoints at T0+6h, T0+28h, T0+29.5h
END = START + timedelta(hours=1)
TERMINAL = END + timedelta(days=2)
OTHER_FACULTY = "manoj.pillai@campusnexus.local"
COMM_KEYS = {"mission_id", "student_id", "followup_id", "purpose", "urgency", "preferred_channel", "not_before"}


def use_brain(app, brain):
    app.state.agent_runtime = build_agent_runtime(
        brain, clock=lambda: app.state.clock(), guardian_policy=POLICY, exam_policy=EXAM_POLICY,
        attendance_policy=ATT_POLICY)
    return app.state.agent_runtime


@pytest.fixture()
def guardians(app, clock):
    return use_brain(app, MockAgentBrain())


# --- Exam helpers --------------------------------------------------------------------------------------------------


def create_exam(client, teaching_id, *, who=A_FACULTY, start=START, **extra):
    return client.post("/exams", headers=auth(client, who), json={
        "teaching_assignment_id": teaching_id, "title": "Mid-term 1", "scheduled_at": start.isoformat(),
        "duration_minutes": 60, "exam_type": "mid", **extra})


def scheduled_exam(client, small_class, **kwargs) -> dict:
    created = create_exam(client, small_class["teaching_id"], **kwargs)
    assert created.status_code == 201, created.text
    response = client.post(f"/exams/{created.json()['id']}/schedule", headers=auth(client, A_FACULTY))
    assert response.status_code == 200, response.text
    return response.json()


def mark(client, exam_id, marks: dict, who=A_FACULTY):
    return client.post(f"/exams/{exam_id}/attendance", headers=auth(client, who),
                       json={"marks": [{"student_code": c, "status": s} for c, s in marks.items()]})


def exam_followups(session_factory, exam_id, purpose=None):
    where = [ExamFollowup.exam_id == exam_id] + ([ExamFollowup.purpose == purpose] if purpose else [])
    return rows(session_factory, ExamFollowup, *where)


def deliver_all(session_factory, model) -> None:
    """Phase 5 will close requests once delivered; simulate that here."""
    with session_factory() as s:
        for row in s.execute(select(model).where(model.status == FollowupStatus.REQUESTED)).scalars():
            row.status = FollowupStatus.CANCELLED
        s.commit()


# --- 1-4: exam lifecycle, snapshot and isolation -------------------------------------------------------------------


def test_faculty_creates_and_schedules_an_exam_with_one_guardian(client, small_class, clock, session_factory) -> None:
    created = create_exam(client, small_class["teaching_id"])
    assert created.status_code == 201, created.text
    assert (created.json()["status"], created.json()["course_code"], created.json()["ends_at"]) == (
        "draft", "GRD301", END.isoformat().replace("+00:00", "Z"))
    eid = created.json()["id"]
    result = client.post(f"/exams/{eid}/schedule", headers=auth(client, A_FACULTY)).json()
    assert (result["created"], result["target_count"], result["exam"]["status"]) == (True, 3, "scheduled")
    mission, steps = mission_of(session_factory, result["guardian_mission_id"])
    assert (mission.agent_key, mission.status, steps, mission.next_wake_at) == (EXAM_KEY, AgentMissionStatus.PENDING, [], T0)
    assert mission.context["inputs"] == {"exam_id": eid}
    assert audit_types(session_factory, "exam", eid) == ["EXAM_CREATED", "EXAM_SCHEDULED", "EXAM_MISSION_CREATED"]
    events = rows(session_factory, DomainEvent, DomainEvent.event_type == "EXAM_SCHEDULED")
    assert len(events) == 1 and events[0].payload["target_count"] == 3
    again = client.post(f"/exams/{eid}/schedule", headers=auth(client, A_FACULTY)).json()
    assert (again["created"], again["guardian_mission_id"]) == (False, result["guardian_mission_id"])
    with session_factory() as s:
        assert s.execute(select(func.count(AgentMission.id)).where(AgentMission.agent_key == EXAM_KEY)).scalar_one() == 1
        assert s.execute(select(func.count(ExamTarget.id)).where(ExamTarget.exam_id == eid)).scalar_one() == 3


def test_schedule_snapshots_the_roster_once(client, small_class, clock, session_factory, orgs) -> None:
    eid = scheduled_exam(client, small_class)["exam"]["id"]
    with session_factory() as s:
        expected = {st.id for st in roster(s, s.get(TeachingAssignment, small_class["teaching_id"]))}
        teaching = s.get(TeachingAssignment, small_class["teaching_id"])
        user = User(organization_id=orgs["a"], email="late@meridian.edu", full_name="Late Joiner", role=UserRole.STUDENT)
        s.add(user)
        s.flush()
        late = Student(organization_id=orgs["a"], student_code="GRD-099", user_id=user.id,
                       department_id=teaching.department_id, year=3, semester=5, cgpa=7.0, section="G")
        s.add(late)
        s.flush()
        s.add(Enrollment(organization_id=orgs["a"], student_id=late.id, course_id=teaching.course_id,
                         academic_year="2026-2027", semester=5))
        s.commit()
    targets = rows(session_factory, ExamTarget, ExamTarget.exam_id == eid)
    assert {t.student_id for t in targets} == expected and sorted(t.student_code for t in targets) == list(SMALL)


def test_exam_bodies_refuse_identity_and_org_overrides(client, small_class, clock) -> None:
    assert create_exam(client, small_class["teaching_id"], organization_id=2).status_code == 422
    assert create_exam(client, small_class["teaching_id"], created_by_account_id=1).status_code == 422
    assert create_exam(client, small_class["teaching_id"], scheduled_at="2026-10-09T10:00:00").status_code == 422  # naive
    assert create_exam(client, small_class["teaching_id"], who=A_STUDENT).status_code == 403
    eid = scheduled_exam(client, small_class)["exam"]["id"]
    response = client.post(f"/exams/{eid}/attendance", headers=auth(client, A_FACULTY), json={
        "marks": [{"student_code": "GRD-001", "status": "exempt", "marked_by_account_id": 1}]})
    assert response.status_code == 422
    assert client.post("/agentos/missions", headers=auth(client, A_FACULTY), json={
        "agent_key": EXAM_KEY, "goal": "watch", "context": {"exam_id": eid}}).json()["detail"]["code"] == "AGENT_NOT_USER_CREATABLE"


def test_exam_cross_access_is_blocked(client, small_class, clock, session_factory) -> None:
    eid = scheduled_exam(client, small_class)["exam"]["id"]
    for who in (B_FACULTY, B_ADMIN, OTHER_FACULTY):
        for path in (f"/exams/{eid}", f"/exams/{eid}/progress"):
            assert client.get(path, headers=auth(client, who)).status_code == 404
        for action in ("schedule", "start", "complete", "cancel"):
            assert client.post(f"/exams/{eid}/{action}", headers=auth(client, who)).status_code == 404
        assert mark(client, eid, {"GRD-001": "exempt"}, who=who).status_code == 404
    assert client.get(f"/exams/{eid}", headers=auth(client, A_ADMIN)).status_code == 200
    # Students: only targeted ones see it, only through their own token; they cannot mark.
    assert client.get(f"/exams/{eid}", headers=auth(client, A_STUDENT)).status_code == 404
    assert client.get(f"/exams/{eid}", headers=auth(client, B_STUDENT)).status_code == 404
    assert client.get("/exams/my", headers=auth(client, B_STUDENT)).json() == []
    assert client.get(f"/exams/{eid}", headers=auth(client, email("GRD-001"))).status_code == 200
    assert [e["id"] for e in client.get("/exams/my", headers=auth(client, email("GRD-001"))).json()] == [eid]
    assert client.get(f"/exams/{eid}/progress", headers=auth(client, email("GRD-001"))).status_code == 403
    assert mark(client, eid, {"GRD-001": "exempt"}, who=email("GRD-001")).status_code == 403


def test_a_draft_exam_is_invisible_to_students_and_their_schedule(client, small_class, clock, session_factory, orgs) -> None:
    from app.services import academic as academic_service

    created = create_exam(client, small_class["teaching_id"]).json()
    assert client.get(f"/exams/{created['id']}", headers=auth(client, email("GRD-001"))).status_code == 404
    with TenantSessionFactory.from_sessionmaker(session_factory).open_tenant_session(orgs["a"]) as s:
        assert all(e.exam_type != "mid" for e in academic_service.get_exam_schedule(s, "GRD-001"))
    client.post(f"/exams/{created['id']}/schedule", headers=auth(client, A_FACULTY))
    with TenantSessionFactory.from_sessionmaker(session_factory).open_tenant_session(orgs["a"]) as s:
        assert [e.exam_type for e in academic_service.get_exam_schedule(s, "GRD-001")].count("mid") == 1
        assert all(e.exam_type != "mid" for e in academic_service.get_exam_schedule(s, "STU-DEMO-001"))  # not a target


# --- 5-6: reminders and attendance marking ---------------------------------------------------------------------------


def test_reminder_checkpoints_request_reminders_only_when_policy_allows(client, small_class, guardians, clock,
                                                                        session_factory) -> None:
    result = scheduled_exam(client, small_class)
    eid, mid = result["exam"]["id"], result["guardian_mission_id"]
    run_worker(app=client.app)  # at scheduling: the reminder window is not open yet -> no AI call
    mission, steps = mission_of(session_factory, mid)
    assert (steps, mission.status, mission.next_wake_at) == ([], AgentMissionStatus.WAITING_EVENT, T0 + timedelta(hours=6))

    clock.now = T0 + timedelta(hours=6)  # 24 h before the exam
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert [(s.action_type, s.tool_name) for s in steps] == [("tool", "request_exam_followup"), ("wait", None)]
    first = exam_followups(session_factory, eid, "exam_reminder")
    assert [(f.attempt_number, f.urgency, f.status) for f in first] == [(1, "normal", FollowupStatus.REQUESTED)] * 3
    events = rows(session_factory, DomainEvent, DomainEvent.event_type == "COMMUNICATION_REQUESTED")
    assert len(events) == 3 and all(set(e.payload) == COMM_KEYS | {"exam_id"} for e in events)
    assert mission.next_wake_at == T0 + timedelta(hours=28)

    deliver_all(session_factory, ExamFollowup)
    clock.now = T0 + timedelta(hours=28)  # 2 h before: a second reminder, high urgency
    run_worker(app=client.app)
    second = [f for f in exam_followups(session_factory, eid, "exam_reminder") if f.attempt_number == 2]
    assert [(f.urgency, f.status) for f in second] == [("high", FollowupStatus.REQUESTED)] * 3

    clock.now = T0 + timedelta(hours=29, minutes=30)  # 30 min before: still active (undelivered) -> no AI call
    steps_before = len(mission_of(session_factory, mid)[1])
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert len(steps) == steps_before and len(exam_followups(session_factory, eid)) == 6
    assert mission.next_wake_at == START + EXAM_POLICY.start_grace


def test_exam_attendance_marking_is_safe_and_idempotent(client, small_class, clock, session_factory) -> None:
    eid = scheduled_exam(client, small_class)["exam"]["id"]
    refused = mark(client, eid, {"GRD-001": "present"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "EXAM_NOT_STARTED"
    assert mark(client, eid, {"GRD-003": "exempt"}).json() == {"exam_id": eid, "changed": 1, "unchanged": 0}
    clock.now = START - timedelta(minutes=16)
    assert client.post(f"/exams/{eid}/start", headers=auth(client, A_FACULTY)).json()["detail"]["code"] == "TOO_EARLY_TO_START"
    clock.now = START - timedelta(minutes=15)
    assert client.post(f"/exams/{eid}/start", headers=auth(client, A_FACULTY)).json()["status"] == "in_progress"
    assert mark(client, eid, {"GRD-001": "present"}).json()["changed"] == 1
    assert mark(client, eid, {"GRD-001": "present"}).json() == {"exam_id": eid, "changed": 0, "unchanged": 1}
    assert mark(client, eid, {"STU-DEMO-001": "present"}).json()["detail"]["code"] == "NOT_A_TARGET"
    bad = mark(client, eid, {"GRD-001": "makeup_completed"})
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "MAKEUP_REQUIRES_ABSENCE"
    conflicting = client.post(f"/exams/{eid}/attendance", headers=auth(client, A_FACULTY), json={"marks": [
        {"student_code": "GRD-002", "status": "present"}, {"student_code": "GRD-002", "status": "absent"}]})
    assert conflicting.json()["detail"]["code"] == "CONFLICTING_MARKS"
    assert mark(client, eid, {"GRD-002": "absent"}).json()["changed"] == 1
    assert mark(client, eid, {"GRD-002": "makeup_completed"}).json()["changed"] == 1
    final = mark(client, eid, {"GRD-002": "absent"})
    assert final.status_code == 409 and final.json()["detail"]["code"] == "MAKEUP_ALREADY_RECORDED"
    marks = {r.student_id: r.status for r in rows(session_factory, ExamAttendance, ExamAttendance.exam_id == eid)}
    sid = small_class["students"]
    assert marks == {sid["GRD-001"]: ExamAttendanceStatus.PRESENT, sid["GRD-002"]: ExamAttendanceStatus.MAKEUP_COMPLETED,
                     sid["GRD-003"]: ExamAttendanceStatus.EXEMPT}
    assert audit_types(session_factory, "exam", eid).count("EXAM_ATTENDANCE_MARKED") == 4  # only effective changes
    kinds = [e.event_type for e in rows(session_factory, DomainEvent, DomainEvent.subject_type == "agent_mission")]
    assert kinds.count("EXAM_ATTENDANCE_MARKED") == 3 and kinds.count("MAKEUP_EXAM_COMPLETED") == 1


# --- 7-13: the Exam Guardian ---------------------------------------------------------------------------------------


def _start_and_mark(client, eid, clock, marks: dict) -> None:
    clock.now = START - timedelta(minutes=5)
    assert client.post(f"/exams/{eid}/start", headers=auth(client, A_FACULTY)).status_code == 200
    clock.now = START + timedelta(minutes=5)
    assert mark(client, eid, marks).status_code == 200


def test_end_to_end_exam_guardian(client, small_class, guardians, clock, session_factory) -> None:
    """absent -> one permitted follow-up (none for present) -> makeup -> follow-up closed -> verified COMPLETE."""
    result = scheduled_exam(client, small_class)
    eid, mid, sid = result["exam"]["id"], result["guardian_mission_id"], small_class["students"]
    _start_and_mark(client, eid, clock, {"GRD-001": "present", "GRD-002": "present", "GRD-003": "absent"})
    mission, _ = mission_of(session_factory, mid)
    assert mission.next_wake_at <= clock.now  # the marking woke the Guardian
    run_worker(app=client.app)  # inside the start grace period: nothing to decide, no AI call
    mission, steps = mission_of(session_factory, mid)
    assert steps == [] and mission.next_wake_at == START + timedelta(minutes=15)

    clock.now = START + timedelta(minutes=15)
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert [(s.action_type, s.tool_name) for s in steps] == [
        ("tool", "get_exam_absentees"), ("tool", "request_exam_followup"), ("wait", None)]
    followups = exam_followups(session_factory, eid, "absence_followup")
    assert [(f.student_id, f.attempt_number, f.urgency) for f in followups] == [(sid["GRD-003"], 1, "high")]
    assert exam_followups(session_factory, eid, "exam_reminder") == []  # scheduled after the window: none
    request = rows(session_factory, DomainEvent, DomainEvent.event_type == "COMMUNICATION_REQUESTED")[0]
    assert set(request.payload) == COMM_KEYS | {"exam_id"} and request.payload["student_id"] == sid["GRD-003"]

    clock.now = START + timedelta(minutes=70)
    assert client.post(f"/exams/{eid}/complete", headers=auth(client, A_FACULTY)).json()["status"] == "completed"
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert mission.status == AgentMissionStatus.WAITING_EVENT and len(steps) == 3  # finished, one unresolved

    clock.now = START + timedelta(days=1)
    assert mark(client, eid, {"GRD-003": "makeup_completed"}).json()["changed"] == 1
    assert [f.status for f in exam_followups(session_factory, eid)] == [FollowupStatus.CANCELLED]  # stopped at once
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert mission.status == AgentMissionStatus.COMPLETED and steps[-1].action_type == "verified_complete"
    assert mission.context["outcome"]["result"] == "ALL_RESOLVED"
    assert (mission.context["outcome"]["present_count"], mission.context["outcome"]["makeup_completed_count"]) == (2, 1)
    audits = audit_types(session_factory, "exam", eid)
    assert audits[-1] == "EXAM_COMPLETED" and audits.count("EXAM_FOLLOWUP_REQUESTED") == 1


def _exam_request(session_factory, org, mid, eid, student_ids, purpose, now, policy=EXAM_POLICY):
    with TenantSessionFactory.from_sessionmaker(session_factory).open_tenant_session(org) as s:
        out = exam_service.request_followups(s, mission=s.get(AgentMission, mid), exam=s.get(Exam, eid),
                                             student_ids=student_ids, purpose=purpose, preferred_channel=None,
                                             policy=policy, now=now, actor_account_id=1)
        s.commit()
        return out


def test_present_and_exempt_students_get_no_follow_up(client, small_class, clock, session_factory, orgs) -> None:
    result = scheduled_exam(client, small_class)
    eid, mid, sid = result["exam"]["id"], result["guardian_mission_id"], small_class["students"]
    _start_and_mark(client, eid, clock, {"GRD-001": "present", "GRD-002": "exempt"})
    out = _exam_request(session_factory, orgs["a"], mid, eid, [sid["GRD-001"], sid["GRD-002"], sid["GRD-003"]],
                        "absence_followup", START + timedelta(minutes=20))
    assert [r["reason"] for r in out] == ["STUDENT_PRESENT", "STUDENT_EXEMPT", "ABSENCE_NOT_CONFIRMED"]
    reminder = _exam_request(session_factory, orgs["a"], mid, eid, None, "exam_reminder", START + timedelta(minutes=20))
    assert {r["reason"] for r in reminder} == {"STUDENT_EXEMPT", "EXAM_ALREADY_STARTED"}
    assert exam_followups(session_factory, eid) == []
    assert audit_types(session_factory, "exam", eid).count("EXAM_FOLLOWUP_SUPPRESSED") == 6


def test_exam_follow_up_cooldown_max_attempts_and_quiet_hours(client, small_class, clock, session_factory, orgs) -> None:
    result = scheduled_exam(client, small_class)
    eid, mid, sid = result["exam"]["id"], result["guardian_mission_id"], small_class["students"]["GRD-003"]
    _start_and_mark(client, eid, clock, {"GRD-003": "absent"})
    now = START + timedelta(minutes=15)
    early = _exam_request(session_factory, orgs["a"], mid, eid, [sid], "absence_followup", now - timedelta(seconds=1))
    assert early[0]["reason"] == "GRACE_PERIOD_NOT_OVER"
    assert _exam_request(session_factory, orgs["a"], mid, eid, [sid], "absence_followup", now)[0]["result"] == "requested"
    assert _exam_request(session_factory, orgs["a"], mid, eid, [sid], "absence_followup", now)[0]["reason"] == "ACTIVE_FOLLOWUP_EXISTS"
    deliver_all(session_factory, ExamFollowup)
    cooldown = EXAM_POLICY.absence.cooldown
    assert _exam_request(session_factory, orgs["a"], mid, eid, [sid], "absence_followup",
                         now + cooldown - timedelta(seconds=1))[0]["reason"] == "COOLDOWN_ACTIVE"
    assert _exam_request(session_factory, orgs["a"], mid, eid, [sid], "absence_followup", now + cooldown)[0]["attempt_number"] == 2
    deliver_all(session_factory, ExamFollowup)
    assert _exam_request(session_factory, orgs["a"], mid, eid, [sid], "absence_followup",
                         now + 3 * cooldown)[0]["reason"] == "MAX_ATTEMPTS_REACHED"
    # Quiet hours (UTC 21:00-07:00 here): deferred to their end, or refused when that is past the terminal deadline.
    quiet = ExamPolicy(absence=FollowupPolicy(max_attempts=5, quiet_start=time(21, 0), quiet_end=time(7, 0)),
                       reminder=EXAM_POLICY.reminder, terminal_after_end=timedelta(days=2))
    night = datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc)
    deferred = _exam_request(session_factory, orgs["a"], mid, eid, [sid], "absence_followup", night, policy=quiet)[0]
    assert deferred["result"] == "requested" and deferred["deferred_for_quiet_hours"] is True
    with session_factory() as s:
        latest = s.execute(select(ExamFollowup).order_by(ExamFollowup.id.desc())).scalars().first()
        assert latest.not_before_at == datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)
    decision = exam_rules.evaluate_exam_followup(
        now=TERMINAL - timedelta(hours=2), purpose="absence_followup", exam_status=ExamStatus.COMPLETED, start=START,
        terminal=TERMINAL, is_target=True, attendance=ExamAttendanceStatus.ABSENT, active_followup=False,
        attempts_so_far=0, last_requested_at=None,
        policy=ExamPolicy(absence=FollowupPolicy(quiet_start=time(0, 0), quiet_end=time(23, 59))))
    assert decision.reason == "QUIET_HOURS_UNTIL_DEADLINE"


def test_the_model_cannot_complete_an_exam_early_or_bypass_policy(client, small_class, app, clock, session_factory) -> None:
    sid = small_class["students"]
    brain = ScriptedAgentBrain([
        AgentDecision(kind=DecisionKind.TOOL, tool_name="request_exam_followup",
                      tool_input={"purpose": "absence_followup", "student_ids": [sid["GRD-001"], 999999]}),
        AgentDecision(kind=DecisionKind.COMPLETE, outcome="Everyone sat the exam.", user_message="Done."),
    ])
    use_brain(app, brain)
    result = scheduled_exam(client, small_class)
    eid, mid = result["exam"]["id"], result["guardian_mission_id"]
    _start_and_mark(client, eid, clock, {"GRD-001": "present", "GRD-002": "absent"})
    clock.now = START + timedelta(minutes=15)
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert [r["reason"] for r in steps[0].output_summary["data"]["results"]] == ["STUDENT_PRESENT", "NOT_A_TARGET"]
    assert (steps[1].status, steps[1].output_summary["error_code"]) == (AgentStepStatus.REJECTED, "COMPLETION_NOT_VERIFIED")
    assert mission.status == AgentMissionStatus.WAITING_EVENT and exam_followups(session_factory, eid) == []
    facts = brain.seen[0].state["supervisor"]
    assert (facts["absent_count"], facts["unmarked_count"], facts["followup_purpose"]) == (1, 1, "absence_followup")


def test_unresolved_terminal_deadline_is_not_a_success(client, small_class, guardians, clock, session_factory) -> None:
    result = scheduled_exam(client, small_class)
    eid, mid = result["exam"]["id"], result["guardian_mission_id"]
    _start_and_mark(client, eid, clock, {"GRD-001": "present", "GRD-002": "present", "GRD-003": "absent"})
    clock.now = START + timedelta(minutes=15)
    run_worker(app=client.app)
    assert len(exam_followups(session_factory, eid)) == 1
    clock.now = TERMINAL + timedelta(seconds=30)  # past the deadline, before its settle point
    run_worker(app=client.app)
    assert mission_of(session_factory, mid)[0].status == AgentMissionStatus.WAITING_EVENT
    clock.now = TERMINAL + timedelta(seconds=61)
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert mission.status == AgentMissionStatus.FAILED and steps[-1].action_type == "verified_fail"
    assert (mission.context["outcome"]["result"], mission.context["outcome"]["unresolved_count"]) == (
        "UNRESOLVED_AT_TERMINAL_DEADLINE", 1)
    assert [f.status for f in exam_followups(session_factory, eid)] == [FollowupStatus.CANCELLED]
    audits = audit_types(session_factory, "exam", eid)
    assert "EXAM_UNRESOLVED" in audits and "EXAM_COMPLETED" not in audits


def test_cancelling_an_exam_stops_the_guardian(client, small_class, guardians, clock, session_factory) -> None:
    result = scheduled_exam(client, small_class)
    eid, mid = result["exam"]["id"], result["guardian_mission_id"]
    clock.now = T0 + timedelta(hours=6)
    run_worker(app=client.app)
    assert len(exam_followups(session_factory, eid)) == 3
    assert client.post(f"/exams/{eid}/cancel", headers=auth(client, A_FACULTY)).json()["status"] == "cancelled"
    assert {f.status for f in exam_followups(session_factory, eid)} == {FollowupStatus.CANCELLED}
    assert mark(client, eid, {"GRD-001": "exempt"}).json()["detail"]["code"] == "EXAM_NOT_ACTIVE"
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert (mission.status, mission.context["outcome"]["result"], steps[-1].action_type) == (
        AgentMissionStatus.CANCELLED, "EXAM_CANCELLED", "verified_cancel")


# --- Attendance helpers ------------------------------------------------------------------------------------------------


CLASS_START = T0 + timedelta(minutes=10)


def class_session(session_factory, orgs, small_class, start=CLASS_START) -> int:
    with session_factory() as s:
        teaching = s.get(TeachingAssignment, small_class["teaching_id"])
        row = AttendanceSession(organization_id=orgs["a"], teaching_assignment_id=teaching.id, course_id=teaching.course_id,
                                faculty_id=teaching.faculty_id, session_date=local(start).date(), scheduled_start=start,
                                scheduled_end=start + timedelta(hours=1), room="G-101",
                                status=AttendanceSessionStatus.SCHEDULED)
        s.add(row)
        s.commit()
        return row.id


def start_class(client, clock, session_id, at=CLASS_START) -> None:
    clock.now = at
    response = client.post(f"/faculty/classes/{session_id}/start", headers=auth(client, A_FACULTY))
    assert response.status_code == 200, response.text


def mark_class(client, session_id, marks: dict) -> None:
    response = client.post(f"/faculty/classes/{session_id}/attendance", headers=auth(client, A_FACULTY),
                           json={"marks": [{"student_id": c, "status": s} for c, s in marks.items()]})
    assert response.status_code == 200, response.text


def scan(client, clock):
    return scan_attendance(client.app.state.session_factory, client.app.state.agent_runtime, now=clock.now)


def interventions(session_factory, *where):
    return rows(session_factory, AttendanceIntervention, *where)


def absent_class(client, clock, session_factory, orgs, small_class, *, third="unmarked"):
    """Class started; GRD-001/002 present; GRD-003 unmarked (or marked ``third``); scanned after the grace period."""
    sid = class_session(session_factory, orgs, small_class)
    start_class(client, clock, sid)
    clock.now = CLASS_START + timedelta(minutes=1)
    marks = {"GRD-001": "present", "GRD-002": "present"}
    if third != "unmarked":
        marks["GRD-003"] = third
    mark_class(client, sid, marks)
    clock.now = CLASS_START + timedelta(minutes=16)
    return sid, scan(client, clock)


# --- 14-22: the Attendance Guardian ---------------------------------------------------------------------------------


def test_absence_before_the_grace_period_does_nothing(client, small_class, guardians, clock, session_factory, orgs) -> None:
    sid = class_session(session_factory, orgs, small_class)
    start_class(client, clock, sid)
    clock.now = CLASS_START + timedelta(minutes=1)
    mark_class(client, sid, {"GRD-001": "present", "GRD-002": "present"})
    clock.now = CLASS_START + timedelta(minutes=14, seconds=59)
    report = scan(client, clock)
    assert (report.events_consumed, report.interventions_created) == (0, 0)
    assert interventions(session_factory) == []
    with TenantSessionFactory.from_sessionmaker(session_factory).open_tenant_session(orgs["a"]) as s:
        results = attendance_monitor.detect_absences(s, client.app.state.agent_runtime, orgs["a"],
                                                     s.get(AttendanceSession, sid), clock.now, ATT_POLICY)
        assert {r.code for r in results} == {"GRACE_NOT_ELAPSED"}  # the grace check comes first
        s.rollback()
    kinds = [e.event_type for e in rows(session_factory, DomainEvent, DomainEvent.subject_type == "attendance_session")]
    assert kinds == ["CLASS_STARTED", "ATTENDANCE_MARKED", "ATTENDANCE_MARKED"]


def test_a_class_whose_roll_has_not_started_creates_nothing(client, small_class, guardians, clock, session_factory, orgs) -> None:
    sid = class_session(session_factory, orgs, small_class)
    start_class(client, clock, sid)
    clock.now = CLASS_START + timedelta(minutes=16)
    report = scan(client, clock)
    assert (report.interventions_created, report.skipped_codes) == (0, {"ROLL_NOT_TAKEN": 3})


def test_confirmed_unexplained_absence_creates_one_intervention(client, small_class, guardians, clock, session_factory,
                                                                orgs) -> None:
    sid, report = absent_class(client, clock, session_factory, orgs, small_class)
    assert (report.interventions_created, report.skipped_codes) == (1, {"ATTENDANCE_RECORDED": 2})
    [case] = interventions(session_factory)
    assert (case.student_id, case.status, case.detected_mark) == (small_class["students"]["GRD-003"],
                                                                 InterventionStatus.OPEN, "unmarked")
    mission, steps = mission_of(session_factory, case.mission_id)
    with session_factory() as s:
        owner = s.execute(select(AuthAccount.id).where(AuthAccount.email == A_FACULTY)).scalar_one()
    assert (mission.agent_key, mission.owner_account_id, mission.status) == (ATTENDANCE_KEY, owner, AgentMissionStatus.PENDING)
    assert audit_types(session_factory, "attendance_intervention", case.id) == [
        "ATTENDANCE_ABSENCE_DETECTED", "ATTENDANCE_MISSION_CREATED"]
    assert len(rows(session_factory, DomainEvent, DomainEvent.event_type == "ABSENCE_DETECTED")) == 1
    # Duplicates: a re-scan consumes nothing new; a direct re-detection is refused; the database refuses a second OPEN row.
    assert scan(client, clock).events_consumed == 0
    with TenantSessionFactory.from_sessionmaker(session_factory).open_tenant_session(orgs["a"]) as s:
        again = attendance_monitor.detect_absences(s, client.app.state.agent_runtime, orgs["a"],
                                                   s.get(AttendanceSession, sid), clock.now, ATT_POLICY)
        assert [r.code for r in again if r.student_id == case.student_id] == ["INTERVENTION_EXISTS"]
        s.rollback()
    with session_factory() as s:
        s.add(AttendanceIntervention(organization_id=orgs["a"], student_id=case.student_id, attendance_session_id=sid,
                                     teaching_assignment_id=case.teaching_assignment_id, course_id=case.course_id,
                                     detected_mark="absent", detected_at=clock.now))
        with pytest.raises(IntegrityError):
            s.commit()
    assert len(interventions(session_factory)) == 1


def _approved_leave(session_factory, orgs, code, start, end, kind=WorkflowRequestType.LEAVE_REQUEST) -> WorkflowRequest:
    with session_factory() as s:
        account = s.execute(select(AuthAccount.id).where(AuthAccount.email == email(code))).scalar_one()
        request = WorkflowRequest(
            organization_id=orgs["a"], request_code=f"REQ-{code}", request_type=kind,
            status=WorkflowRequestStatus.APPROVED, requester_account_id=account, requester_role="student", student_id=code,
            title="Leave", reason="Family function", routing_basis="mentor", routing_note="test",
            context={"window_start": start.isoformat(), "window_end": end.isoformat()})
        s.add(request)
        s.commit()
        s.expunge(request)
        return request


def test_approved_leave_prevents_an_intervention(client, small_class, guardians, clock, session_factory, orgs) -> None:
    _approved_leave(session_factory, orgs, "GRD-003", CLASS_START - timedelta(hours=2), CLASS_START + timedelta(minutes=5))
    _, report = absent_class(client, clock, session_factory, orgs, small_class)
    assert (report.interventions_created, report.skipped_codes) == (0, {"ATTENDANCE_RECORDED": 2, "LEAVE_APPROVED": 1})


def test_attendance_guardian_requests_a_follow_up_and_correction_resolves(client, small_class, guardians, clock,
                                                                         session_factory, orgs) -> None:
    sid, _ = absent_class(client, clock, session_factory, orgs, small_class, third="absent")
    [case] = interventions(session_factory)
    assert case.detected_mark == "absent"
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, case.mission_id)
    assert [(s.action_type, s.tool_name) for s in steps] == [
        ("tool", "get_attendance_case"), ("tool", "request_attendance_followup"), ("wait", None)]
    assert mission.next_wake_at == clock.now + ATT_POLICY.contact.cooldown
    [request] = rows(session_factory, DomainEvent, DomainEvent.event_type == "COMMUNICATION_REQUESTED")
    assert set(request.payload) == COMM_KEYS | {"intervention_id", "session_id"}

    clock.now = CLASS_START + timedelta(minutes=30)
    mark_class(client, sid, {"GRD-003": "present"})  # the faculty corrects the mark
    assert mission_of(session_factory, case.mission_id)[0].next_wake_at == clock.now  # the event woke the Guardian
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, case.mission_id)
    assert (mission.status, steps[-1].action_type) == (AgentMissionStatus.COMPLETED, "verified_complete")
    assert mission.context["outcome"] == {"result": "RESOLVED", "resolution_code": "ATTENDANCE_CORRECTED",
                                          "intervention_id": case.id, "student_id": case.student_id}
    [case] = interventions(session_factory)
    assert (case.status, case.resolution_code) == (InterventionStatus.RESOLVED, "ATTENDANCE_CORRECTED")
    assert [f.status for f in rows(session_factory, AttendanceFollowup)] == [FollowupStatus.CANCELLED]
    assert audit_types(session_factory, "attendance_intervention", case.id)[-1] == "ATTENDANCE_RESOLVED"


def test_justification_and_leave_approval_resolve(client, small_class, guardians, clock, session_factory, orgs) -> None:
    absent_class(client, clock, session_factory, orgs, small_class)
    [case] = interventions(session_factory)
    run_worker(app=client.app)
    bad = client.post(f"/attendance/interventions/{case.id}/justify", headers=auth(client, A_FACULTY),
                      json={"justification_code": "BECAUSE", "approved_by": 1})
    assert bad.status_code == 422
    clock.now += timedelta(minutes=5)
    ok = client.post(f"/attendance/interventions/{case.id}/justify", headers=auth(client, A_FACULTY),
                     json={"justification_code": "MEDICAL"})
    assert ok.status_code == 200 and ok.json()["justification_code"] == "MEDICAL"
    run_worker(app=client.app)
    [case] = interventions(session_factory)
    assert (case.status, case.resolution_code) == (InterventionStatus.RESOLVED, "ATTENDANCE_JUSTIFIED")
    assert mission_of(session_factory, case.mission_id)[0].status == AgentMissionStatus.COMPLETED


def test_leave_approved_after_detection_resolves(client, small_class, guardians, clock, session_factory, orgs) -> None:
    absent_class(client, clock, session_factory, orgs, small_class)
    [case] = interventions(session_factory)
    run_worker(app=client.app)
    clock.now += timedelta(hours=1)
    request = _approved_leave(session_factory, orgs, "GRD-003", CLASS_START, CLASS_START + timedelta(hours=1),
                              kind=WorkflowRequestType.ATTENDANCE_PERMISSION)
    with TenantSessionFactory.from_sessionmaker(session_factory).open_tenant_session(orgs["a"]) as s:
        assert attendance_monitor.leave_approved(s, s.get(WorkflowRequest, request.id), None, clock.now) == 1
        s.commit()
    assert rows(session_factory, DomainEvent, DomainEvent.event_type == "LEAVE_APPROVED")[0].subject_id == str(case.mission_id)
    run_worker(app=client.app)
    [case] = interventions(session_factory)
    assert (case.status, case.resolution_code) == (InterventionStatus.RESOLVED, "LEAVE_APPROVED")


def test_attendance_follow_up_policy_and_unresolved_window(client, small_class, guardians, clock, session_factory,
                                                          orgs) -> None:
    absent_class(client, clock, session_factory, orgs, small_class)
    [case] = interventions(session_factory)
    detected = case.detected_at
    run_worker(app=client.app)
    assert len(rows(session_factory, AttendanceFollowup)) == 1
    clock.now = detected + ATT_POLICY.contact.cooldown  # checkpoint, but the request is still active: no AI call
    steps_before = len(mission_of(session_factory, case.mission_id)[1])
    run_worker(app=client.app)
    assert len(mission_of(session_factory, case.mission_id)[1]) == steps_before
    deliver_all(session_factory, AttendanceFollowup)
    wake_and_run = lambda: (_wake(session_factory, orgs, case.mission_id, clock.now), run_worker(app=client.app))  # noqa: E731
    wake_and_run()
    assert [f.attempt_number for f in rows(session_factory, AttendanceFollowup)] == [1, 2]
    deliver_all(session_factory, AttendanceFollowup)
    clock.now += timedelta(hours=7)
    steps_before = len(mission_of(session_factory, case.mission_id)[1])
    wake_and_run()  # max attempts reached: nothing to decide, no AI call, no third request
    assert len(mission_of(session_factory, case.mission_id)[1]) == steps_before
    assert len(rows(session_factory, AttendanceFollowup)) == 2
    clock.now = detected + ATT_POLICY.resolution_window + timedelta(seconds=61)
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, case.mission_id)
    assert (mission.status, mission.context["outcome"]["result"], steps[-1].action_type) == (
        AgentMissionStatus.FAILED, "ABSENCE_UNRESOLVED", "verified_fail")
    [case] = interventions(session_factory)
    assert (case.status, case.resolution_code) == (InterventionStatus.UNRESOLVED, "RESOLUTION_WINDOW_CLOSED")
    assert "ATTENDANCE_UNRESOLVED" in audit_types(session_factory, "attendance_intervention", case.id)
    assert audit_types(session_factory, "attendance_intervention", case.id).count("ATTENDANCE_FOLLOWUP_REQUESTED") == 2


def _wake(session_factory, orgs, mission_id, now) -> None:
    with TenantSessionFactory.from_sessionmaker(session_factory).open_tenant_session(orgs["a"]) as s:
        wake_mission(s, mission_id, now)
        s.commit()


def test_attendance_interventions_are_tenant_and_class_scoped(client, small_class, guardians, clock, session_factory,
                                                              orgs) -> None:
    absent_class(client, clock, session_factory, orgs, small_class)
    [case] = interventions(session_factory)
    for who in (B_FACULTY, B_ADMIN, OTHER_FACULTY):
        assert client.get(f"/attendance/interventions/{case.id}", headers=auth(client, who)).status_code == 404
        assert client.get("/attendance/interventions", headers=auth(client, who)).json() == []
        assert client.post(f"/attendance/interventions/{case.id}/justify", headers=auth(client, who),
                           json={"justification_code": "MEDICAL"}).status_code == 404
    assert client.get("/attendance/interventions", headers=auth(client, A_STUDENT)).status_code == 403
    assert client.get(f"/attendance/interventions/{case.id}", headers=auth(client, A_ADMIN)).status_code == 200
    mine = client.get("/attendance/interventions?status=open", headers=auth(client, A_FACULTY)).json()
    assert [(i["id"], i["student_code"], i["current_mark"]) for i in mine] == [(case.id, "GRD-003", None)]
    # The worker's tenant sessions: organization B's worker never sees organization A's mission.
    factory = TenantSessionFactory.from_sessionmaker(session_factory)
    with factory.open_tenant_session(orgs["b"]) as s:
        assert s.get(AgentMission, case.mission_id) is None and s.get(AttendanceIntervention, case.id) is None


def test_no_contact_data_in_events_or_audit(client, small_class, guardians, clock, session_factory, orgs) -> None:
    result = scheduled_exam(client, small_class)
    _start_and_mark(client, result["exam"]["id"], clock, {"GRD-001": "present", "GRD-002": "present", "GRD-003": "absent"})
    clock.now = START + timedelta(minutes=15)
    run_worker(app=client.app)
    clock.now = START + timedelta(hours=3)
    absent_class(client, clock, session_factory, orgs, small_class)
    clock.now = CLASS_START + timedelta(minutes=16)
    run_worker(app=client.app)
    events = rows(session_factory, DomainEvent)
    assert len([e for e in events if e.event_type == "COMMUNICATION_REQUESTED"]) == 2
    text = json.dumps([e.payload for e in events]) + json.dumps(
        [a.event_metadata for a in rows(session_factory, OperationAuditEvent)
         if a.subject_type in ("exam", "attendance_intervention", "agent_mission")])
    assert "@" not in text and "+91" not in text and "meridian" not in text and "Student GRD" not in text
    for model in (ExamFollowup, AttendanceFollowup, AttendanceIntervention, ExamAttendance, ExamTarget):
        assert not {"phone", "email", "mobile", "full_name"} & set(model.__table__.columns.keys())


# --- 23-26: worker and kernel ----------------------------------------------------------------------------------------


def _membership(session_factory, who, status) -> None:
    with session_factory() as s:
        account = s.execute(select(AuthAccount.id).where(AuthAccount.email == who)).scalar_one()
        s.execute(select(OrganizationMembership).where(OrganizationMembership.account_id == account)).scalar_one().status = status
        s.commit()


def test_inactive_owner_is_parked_once_without_retries(client, small_class, guardians, clock, session_factory, orgs) -> None:
    mid = scheduled_exam(client, small_class)["guardian_mission_id"]
    _membership(session_factory, A_FACULTY, MembershipStatus.INACTIVE)
    run = run_worker(app=client.app).runs[0]
    mission, steps = mission_of(session_factory, mid)
    assert (run.outcome, run.error_code, run.status) == ("skipped", OWNER_INACTIVE, "waiting_human")
    assert (mission.status, mission.waiting_for, mission.next_wake_at, steps) == (
        AgentMissionStatus.WAITING_HUMAN, OWNER_INACTIVE, None, [])
    clock.advance(hours=3)
    assert run_worker(app=client.app).runs == []  # no wake time: no retry every few minutes
    _wake(session_factory, orgs, mid, clock.now)  # a domain event wakes it: still inactive -> re-parked silently
    assert run_worker(app=client.app).runs[0].error_code == OWNER_INACTIVE
    assert mission_of(session_factory, mid)[0].next_wake_at is None
    assert audit_types(session_factory, "agent_mission", mid).count("OWNER_INACTIVE_MISSION_PARKED") == 1
    _membership(session_factory, A_FACULTY, MembershipStatus.ACTIVE)
    _wake(session_factory, orgs, mid, clock.now)
    run = run_worker(app=client.app).runs[0]
    assert (run.outcome, run.status) == ("processed", "waiting_event")  # resumed normally, never impersonated


def test_due_worker_handles_both_new_agents(client, small_class, guardians, clock, session_factory, orgs) -> None:
    exam_mission = scheduled_exam(client, small_class)["guardian_mission_id"]
    absent_class(client, clock, session_factory, orgs, small_class)
    [case] = interventions(session_factory)
    report = run_worker(app=client.app)
    assert {(r.mission_id, r.agent_key, r.outcome) for r in report.runs} == {
        (exam_mission, EXAM_KEY, "processed"), (case.mission_id, ATTENDANCE_KEY, "processed")}


def test_worker_stays_bounded_for_the_exam_guardian(client, small_class, app, clock, session_factory) -> None:
    looping = ScriptedAgentBrain([AgentDecision(kind=DecisionKind.TOOL, tool_name="get_exam_progress")] * 50)
    use_brain(app, looping)
    soon = T0 + timedelta(hours=1)  # inside the reminder window: the brain is asked
    first = scheduled_exam(client, small_class, start=soon)["guardian_mission_id"]
    second = scheduled_exam(client, small_class, start=soon)["guardian_mission_id"]
    report = run_worker(app=client.app, limit=1, max_transitions=3)
    assert [(r.mission_id, r.transitions, r.status) for r in report.runs] == [(first, 3, "running")]
    mission, steps = mission_of(session_factory, first)
    assert len(steps) == 3 and mission.next_wake_at == T0 + timedelta(minutes=5) and mission.lease_owner is None
    assert [r.mission_id for r in run_worker(app=client.app, limit=5).runs] == [second]


def test_nexus_exam_and_attendance_tools_are_caller_scoped(client, small_class, guardians, clock, session_factory,
                                                           orgs) -> None:
    from app.agentos.nexus import GetMyAttendanceSummary, GetMyExams, NoInput
    from app.agentos.registry import ToolContext

    eid = scheduled_exam(client, small_class)["exam"]["id"]
    absent_class(client, clock, session_factory, orgs, small_class)
    with TenantSessionFactory.from_sessionmaker(session_factory).open_tenant_session(orgs["a"]) as s:
        def ctx(role, who, **links):
            account = s.execute(select(AuthAccount.id).where(AuthAccount.email == who)).scalar_one()
            return ToolContext(session=s, organization_id=orgs["a"], account_id=account, role=role, mission_id=0,
                               now=clock.now, **links)

        student = ctx(UserRole.STUDENT, email("GRD-003"), student_code="GRD-003")
        assert [e["exam_id"] for e in GetMyExams().execute(student, NoInput()).data["exams"]] == [eid]
        assert GetMyAttendanceSummary().execute(student, NoInput()).data["open_absence_cases"] == 1
        outsider = ctx(UserRole.STUDENT, A_STUDENT, student_code="STU-DEMO-001")
        assert GetMyExams().execute(outsider, NoInput()).data["exams"] == []
        assert GetMyAttendanceSummary().execute(outsider, NoInput()).data["open_absence_cases"] == 0
        faculty = ctx(UserRole.FACULTY, A_FACULTY, faculty_profile_id=small_class["faculty_id"])
        assert [e["exam_id"] for e in GetMyExams().execute(faculty, NoInput()).data["exams"]] == [eid]
        classes = GetMyAttendanceSummary().execute(faculty, NoInput()).data["classes"]
        assert [c["open_absence_cases"] for c in classes if c["teaching_assignment_id"] == small_class["teaching_id"]] == [1]
        with session_factory() as plain:
            other_profile = plain.execute(select(OrganizationMembership.faculty_profile_id).join(
                AuthAccount, AuthAccount.id == OrganizationMembership.account_id).where(
                AuthAccount.email == OTHER_FACULTY)).scalar_one()
        other = ctx(UserRole.FACULTY, OTHER_FACULTY, faculty_profile_id=other_profile)
        assert GetMyExams().execute(other, NoInput()).data["exams"] == []
        assert all(c["open_absence_cases"] == 0 for c in GetMyAttendanceSummary().execute(other, NoInput()).data["classes"])


# --- Deterministic rules (boundaries) -------------------------------------------------------------------------------


def test_shared_contact_limits_order_and_boundaries() -> None:
    policy = FollowupPolicy(cooldown=timedelta(hours=1), max_attempts=2, **NO_QUIET)
    base = dict(now=T0, active_followup=False, attempts_so_far=0, last_requested_at=None, policy=policy, hard_stop=None)
    assert check_contact_limits(**base).attempt_number == 1
    assert check_contact_limits(**{**base, "active_followup": True, "attempts_so_far": 5}).reason == "ACTIVE_FOLLOWUP_EXISTS"
    assert check_contact_limits(**{**base, "attempts_so_far": 2}).reason == "MAX_ATTEMPTS_REACHED"
    last = T0 - timedelta(hours=1)
    assert check_contact_limits(**{**base, "attempts_so_far": 1, "last_requested_at": last}).allowed  # exactly at cooldown
    assert check_contact_limits(**{**base, "attempts_so_far": 1, "last_requested_at": last + timedelta(seconds=1)}).reason == "COOLDOWN_ACTIVE"


def test_exam_rules_boundaries() -> None:
    assert exam_rules.exam_end(START, 60) == END
    assert exam_rules.terminal_deadline(END, None, EXAM_POLICY) == TERMINAL
    assert exam_rules.terminal_deadline(END, END + timedelta(hours=3), EXAM_POLICY) == END + timedelta(hours=3)
    assert not exam_rules.finished(ExamStatus.IN_PROGRESS, END, END - timedelta(seconds=1))
    assert exam_rules.finished(ExamStatus.IN_PROGRESS, END, END) and exam_rules.finished(ExamStatus.COMPLETED, END, START)
    assert not exam_rules.finished(ExamStatus.CANCELLED, END, END + timedelta(days=1))
    assert not exam_rules.all_resolved(0, 0) and exam_rules.all_resolved(3, 3) and not exam_rules.all_resolved(3, 2)
    assert exam_rules.can_start(ExamStatus.SCHEDULED, START, END, START - timedelta(minutes=15)).allowed
    assert exam_rules.can_start(ExamStatus.SCHEDULED, START, END, START - timedelta(minutes=15, seconds=1)).code == "TOO_EARLY_TO_START"
    assert exam_rules.can_start(ExamStatus.SCHEDULED, START, END, END).code == "EXAM_TIME_OVER"
    points = exam_rules.checkpoints(T0, START, END, TERMINAL, EXAM_POLICY)
    assert points == [T0, T0 + timedelta(hours=6), T0 + timedelta(hours=28), T0 + timedelta(hours=29, minutes=30),
                      START + timedelta(minutes=15), END + exam_rules.SETTLE, END + exam_rules.SETTLE + timedelta(hours=6),
                      END + exam_rules.SETTLE + timedelta(hours=12), TERMINAL + exam_rules.SETTLE]
    late = exam_rules.checkpoints(START - timedelta(hours=1), START, END, TERMINAL, EXAM_POLICY)
    assert late[:2] == [START - timedelta(hours=1), START - timedelta(minutes=30)]  # unavailable reminders collapse
    A, M, P, E = (ExamAttendanceStatus.ABSENT, ExamAttendanceStatus.MAKEUP_COMPLETED, ExamAttendanceStatus.PRESENT,
                  ExamAttendanceStatus.EXEMPT)
    assert exam_rules.can_mark(ExamStatus.SCHEDULED, None, E).allowed
    assert exam_rules.can_mark(ExamStatus.SCHEDULED, None, P).code == "EXAM_NOT_STARTED"
    assert exam_rules.can_mark(ExamStatus.COMPLETED, A, M).allowed and exam_rules.can_mark(ExamStatus.COMPLETED, P, P).code == "UNCHANGED"
    assert exam_rules.can_mark(ExamStatus.CANCELLED, None, E).code == "EXAM_NOT_ACTIVE"
    assert exam_rules.can_mark(ExamStatus.COMPLETED, M, A).code == "MAKEUP_ALREADY_RECORDED"


def _exam_followup(**overrides):
    base = dict(now=START + timedelta(minutes=15), purpose="absence_followup", exam_status=ExamStatus.IN_PROGRESS,
                start=START, terminal=TERMINAL, is_target=True, attendance=ExamAttendanceStatus.ABSENT,
                active_followup=False, attempts_so_far=0, last_requested_at=None, policy=EXAM_POLICY)
    return exam_rules.evaluate_exam_followup(**{**base, **overrides})


def test_exam_followup_policy_boundaries() -> None:
    assert _exam_followup().allowed  # exactly at the end of the grace period
    assert _exam_followup(now=START + timedelta(minutes=15) - timedelta(seconds=1)).reason == "GRACE_PERIOD_NOT_OVER"
    assert _exam_followup(now=TERMINAL - timedelta(seconds=1)).allowed
    assert _exam_followup(now=TERMINAL).reason == "TERMINAL_DEADLINE_PASSED"
    assert _exam_followup(is_target=False).reason == "NOT_A_TARGET"
    assert _exam_followup(exam_status=ExamStatus.CANCELLED).reason == "EXAM_NOT_ACTIVE"
    assert _exam_followup(attempts_so_far=2).reason == "MAX_ATTEMPTS_REACHED"
    reminder = dict(purpose="exam_reminder", exam_status=ExamStatus.SCHEDULED, attendance=None)
    assert _exam_followup(now=START - timedelta(hours=24), **reminder).allowed  # the window opens exactly at start - 24 h
    assert _exam_followup(now=START - timedelta(hours=24, seconds=1), **reminder).reason == "REMINDER_WINDOW_NOT_OPEN"
    assert _exam_followup(now=START, **reminder).reason == "EXAM_ALREADY_STARTED"
    assert _exam_followup(now=START - timedelta(hours=2), **reminder).urgency == "high"
    assert _exam_followup(purpose="anything", attendance=None, exam_status=ExamStatus.SCHEDULED).reason == "UNKNOWN_PURPOSE"


def test_attendance_rules_boundaries() -> None:
    started = CLASS_START
    detect = lambda **o: att_rules.detect_absence(**{**dict(  # noqa: E731
        session_status="active", started_at=started, now=started + ATT_POLICY.grace, mark=None, roll_taken=True,
        leave_approved=False, has_intervention=False, policy=ATT_POLICY), **o})
    assert detect() == att_rules.DetectionCode.ABSENCE_CONFIRMED  # exactly at the end of the grace period
    assert detect(now=started + ATT_POLICY.grace - timedelta(seconds=1)) == att_rules.DetectionCode.GRACE_NOT_ELAPSED
    assert detect(session_status="cancelled") == att_rules.DetectionCode.CLASS_CANCELLED
    assert detect(session_status="scheduled") == att_rules.DetectionCode.CLASS_NOT_HELD
    assert detect(session_status="closed", mark="absent") == att_rules.DetectionCode.ABSENCE_CONFIRMED
    assert detect(mark="late") == att_rules.DetectionCode.ATTENDANCE_RECORDED
    assert detect(mark="excused") == att_rules.DetectionCode.EXCUSED
    assert detect(roll_taken=False) == att_rules.DetectionCode.ROLL_NOT_TAKEN
    assert detect(roll_taken=False, mark="absent") == att_rules.DetectionCode.ABSENCE_CONFIRMED
    assert detect(leave_approved=True) == att_rules.DetectionCode.LEAVE_APPROVED
    assert detect(has_intervention=True) == att_rules.DetectionCode.INTERVENTION_EXISTS
    end = started + timedelta(hours=1)
    assert att_rules.leave_covers(started - timedelta(hours=1), started + timedelta(seconds=1), started, end)
    assert not att_rules.leave_covers(started - timedelta(hours=1), started, started, end)  # touching is not overlapping
    assert not att_rules.leave_covers(end, end + timedelta(hours=1), started, end)
    assert att_rules.resolution(mark="present", detected_mark="absent", justification_code=None, leave_approved=False) == "ATTENDANCE_CORRECTED"
    assert att_rules.resolution(mark="late", detected_mark="unmarked", justification_code=None, leave_approved=False) == "ATTENDANCE_RECORDED"
    assert att_rules.resolution(mark="absent", detected_mark="absent", justification_code="MEDICAL", leave_approved=False) == "ATTENDANCE_JUSTIFIED"
    assert att_rules.resolution(mark=None, detected_mark="unmarked", justification_code=None, leave_approved=True) == "LEAVE_APPROVED"
    assert att_rules.resolution(mark="absent", detected_mark="absent", justification_code=None, leave_approved=False) is None
    assert att_rules.checkpoints(started, ATT_POLICY) == [started, started + timedelta(hours=6),
                                                          started + timedelta(hours=24) + att_rules.SETTLE]
    deadline = att_rules.resolution_deadline(started, ATT_POLICY)
    evaluate = lambda **o: att_rules.evaluate_attendance_followup(**{**dict(  # noqa: E731
        now=deadline - timedelta(seconds=1), intervention_open=True, resolved_by=None, deadline=deadline,
        active_followup=False, attempts_so_far=0, last_requested_at=None, policy=ATT_POLICY), **o})
    assert evaluate().allowed and evaluate(now=deadline).reason == "RESOLUTION_WINDOW_CLOSED"
    assert evaluate(resolved_by="LEAVE_APPROVED").reason == "ABSENCE_RESOLVED"
    assert evaluate(intervention_open=False).reason == "INTERVENTION_NOT_OPEN"
    assert evaluate(attempts_so_far=2).reason == "MAX_ATTEMPTS_REACHED"


# --- Terminal outcomes are the supervisor's: a brain COMPLETE/FAIL is never trusted ---------------------------------


TERMINAL_CLAIMS = [
    (AgentDecision(kind=DecisionKind.COMPLETE, outcome="All resolved.", user_message="Done."), "COMPLETION_NOT_VERIFIED"),
    (AgentDecision(kind=DecisionKind.FAIL, reason="Giving up."), "FAIL_NOT_VERIFIED"),
]
CONSISTENT = {  # a terminal Guardian mission <-> the intervention's terminal state
    AgentMissionStatus.COMPLETED: InterventionStatus.RESOLVED,
    AgentMissionStatus.FAILED: InterventionStatus.UNRESOLVED,
    AgentMissionStatus.CANCELLED: InterventionStatus.CANCELLED,
}


def _rejected_claim(session_factory, mid, code):
    mission, steps = mission_of(session_factory, mid)
    assert (steps[-1].status, steps[-1].output_summary["error_code"]) == (AgentStepStatus.REJECTED, code)
    assert mission.status == AgentMissionStatus.WAITING_EVENT and mission.next_wake_at is not None
    assert "outcome" not in mission.context
    return mission


@pytest.mark.parametrize("claim,code", TERMINAL_CLAIMS, ids=["complete", "fail"])
def test_assignment_guardian_refuses_an_unverified_terminal_claim(client, small_class, app, clock, session_factory,
                                                                   claim, code) -> None:
    from tests.test_agentos_assignment_guardian import published, submit
    use_brain(app, ScriptedAgentBrain([claim]))
    result = published(client, small_class)
    aid, mid = result["assignment"]["id"], result["guardian_mission_id"]
    clock.advance(minutes=1)
    run_worker(app=client.app)
    _rejected_claim(session_factory, mid, code)
    for student in ("GRD-001", "GRD-002", "GRD-003"):
        assert submit(client, student, aid).status_code == 201
    run_worker(app=client.app)  # the deterministic success condition now holds: no brain call needed
    mission, steps = mission_of(session_factory, mid)
    assert (mission.status, steps[-1].action_type, mission.context["outcome"]["result"]) == (
        AgentMissionStatus.COMPLETED, "verified_complete", "ALL_SUBMITTED")


@pytest.mark.parametrize("claim,code", TERMINAL_CLAIMS, ids=["complete", "fail"])
def test_exam_guardian_refuses_an_unverified_terminal_claim(client, small_class, app, clock, session_factory,
                                                             claim, code) -> None:
    use_brain(app, ScriptedAgentBrain([claim]))
    result = scheduled_exam(client, small_class)
    eid, mid = result["exam"]["id"], result["guardian_mission_id"]
    _start_and_mark(client, eid, clock, {"GRD-001": "present", "GRD-002": "present", "GRD-003": "absent"})
    clock.now = START + timedelta(minutes=15)
    run_worker(app=client.app)
    _rejected_claim(session_factory, mid, code)
    with session_factory() as s:
        assert s.get(Exam, eid).status == ExamStatus.IN_PROGRESS  # the domain record is untouched
    clock.now = TERMINAL + timedelta(seconds=61)  # only the deterministic terminal deadline fails it
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, mid)
    assert (mission.status, steps[-1].action_type, mission.context["outcome"]["result"]) == (
        AgentMissionStatus.FAILED, "verified_fail", "UNRESOLVED_AT_TERMINAL_DEADLINE")


@pytest.mark.parametrize("claim,code", TERMINAL_CLAIMS, ids=["complete", "fail"])
def test_attendance_guardian_refuses_an_unverified_terminal_claim(client, small_class, app, clock, session_factory,
                                                                   orgs, claim, code) -> None:
    use_brain(app, ScriptedAgentBrain([claim]))
    sid, _ = absent_class(client, clock, session_factory, orgs, small_class)
    [case] = interventions(session_factory)
    run_worker(app=client.app)
    _rejected_claim(session_factory, case.mission_id, code)
    [case] = interventions(session_factory)
    assert (case.status, case.resolution_code, case.resolved_at) == (InterventionStatus.OPEN, None, None)

    if claim.kind == DecisionKind.COMPLETE:  # deterministic COMPLETE: the faculty corrects the mark
        clock.now += timedelta(minutes=10)
        mark_class(client, sid, {"GRD-003": "present"})
        expected = (AgentMissionStatus.COMPLETED, "verified_complete")
    else:  # deterministic FAILED: the resolution window closes unresolved
        clock.now = case.detected_at + ATT_POLICY.resolution_window + timedelta(seconds=61)
        expected = (AgentMissionStatus.FAILED, "verified_fail")
    run_worker(app=client.app)
    mission, steps = mission_of(session_factory, case.mission_id)
    [case] = interventions(session_factory)
    assert (mission.status, steps[-1].action_type) == expected
    assert case.status == CONSISTENT[mission.status] and case.resolved_at is not None
