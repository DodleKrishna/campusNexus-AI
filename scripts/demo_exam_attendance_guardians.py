"""Local end-to-end Exam Guardian + Attendance Guardian run on a throwaway SQLite database (AgentOS V2 Phase 4).

Exam: schedule an exam for a 3-student class -> reminder checkpoint (3 reminder requests) -> start -> 2 present,
1 absent -> the Guardian requests a follow-up for the absent student -> makeup completed -> verified COMPLETE.

Attendance: a class session starts -> 2 students marked present, 1 missing -> after the grace period the
deterministic scan opens one intervention + Attendance Guardian mission -> follow-up requested -> the faculty
corrects the mark -> the Guardian verifies and resolves.

Offline (mock brains), pinned clock, nothing touches the dev database. Prints a structured JSON trace only (ids,
statuses, codes) and exits non-zero if any expectation fails.

    python scripts/demo_exam_attendance_guardians.py
"""
from __future__ import annotations

import json
import secrets
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.agentos.attendance_guardian import scan_attendance  # noqa: E402
from app.agentos.bootstrap import build_agent_runtime  # noqa: E402
from app.agentos.providers import MockAgentBrain  # noqa: E402
from app.agentos.worker import process_due_missions  # noqa: E402
from app.auth.accounts import seed_dev_accounts  # noqa: E402
from app.db.models import (  # noqa: E402
    AgentMission, AgentStep, AttendanceIntervention, AttendanceSession, AttendanceSessionStatus, AuthAccount, DomainEvent,
    FacultyProfile, TeachingAssignment,
)
from app.db.models.exam import ExamAttendanceStatus  # noqa: E402
from app.db.session import create_db_engine, create_session_factory, init_db  # noqa: E402
from app.db.tenant_session import TenantSessionFactory  # noqa: E402
from app.rules.attendance_intervention import AttendancePolicy  # noqa: E402
from app.rules.exam_rules import ExamPolicy  # noqa: E402
from app.rules.followup_policy import FollowupPolicy  # noqa: E402
from app.services import exams, faculty_ops  # noqa: E402
from app.services.class_schedule import local  # noqa: E402
from scripts.demo_assignment_guardian import CODES, build_class  # noqa: E402
from scripts.seed_data import run_seed  # noqa: E402

T0 = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)
NO_QUIET = dict(quiet_start=time(0, 0), quiet_end=time(0, 0))  # quiet hours off: the trace is time-of-day independent
EXAM_POLICY = ExamPolicy(reminder=FollowupPolicy(cooldown=timedelta(minutes=20), max_attempts=3, **NO_QUIET),
                         absence=FollowupPolicy(cooldown=timedelta(hours=6), max_attempts=2, **NO_QUIET))
ATTENDANCE_POLICY = AttendancePolicy(contact=FollowupPolicy(cooldown=timedelta(hours=6), max_attempts=2, **NO_QUIET))
EXAM_START = T0 + timedelta(hours=30)
CLASS_START = EXAM_START + timedelta(days=2)


def trace(step: str, **data) -> None:
    print(json.dumps({"step": step, **data}, default=str, separators=(",", ":")))


def main() -> int:
    clock = {"now": T0}
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_db_engine(db_path=str(Path(tmp) / "guardians_e2e.db"))
        init_db(engine)
        with create_session_factory(engine)() as system_session:
            run_seed(system_session)
            seed_dev_accounts(system_session, secrets.token_urlsafe(16))
            teaching_id, students, faculty = build_class(system_session)
        factory = TenantSessionFactory(engine)
        runtime = build_agent_runtime(MockAgentBrain(), clock=lambda: clock["now"], exam_policy=EXAM_POLICY,
                                      attendance_policy=ATTENDANCE_POLICY)
        org = faculty.organization_id
        ids = {code: students[code][0] for code in CODES}

        def at(value: datetime) -> None:
            clock["now"] = value

        def work(label: str) -> None:
            report = process_due_missions(factory, runtime, now=clock["now"], worker_id="e2e-worker")
            trace(label, runs=[r.model_dump(exclude={"organization_id"}) for r in report.runs])

        def mission_state(mission_id: int) -> dict:
            with factory.open_tenant_session(org) as s:
                m = s.get(AgentMission, mission_id)
                steps = list(s.execute(select(AgentStep.action_type, AgentStep.tool_name)
                                       .where(AgentStep.mission_id == mission_id).order_by(AgentStep.step_number)).all())
                return {"agent": m.agent_key, "status": m.status.value, "waiting_for": m.waiting_for,
                        "next_wake_at": m.next_wake_at, "outcome": (m.context or {}).get("outcome"),
                        "steps": [f"{a}:{t}" if t else a for a, t in steps]}

        def requests(**match) -> list:
            with factory.open_tenant_session(org) as s:
                rows = [e.payload for e in s.execute(select(DomainEvent).where(
                    DomainEvent.event_type == "COMMUNICATION_REQUESTED").order_by(DomainEvent.id)).scalars()]
            return [p for p in rows if all(p.get(k) == v for k, v in match.items())]

        # --- Exam Guardian -----------------------------------------------------------------------------------------
        with factory.open_tenant_session(org) as s:
            exam = exams.create_exam(s, faculty, exams.CreateExam(
                teaching_assignment_id=teaching_id, title="Mid-term 1", scheduled_at=EXAM_START, duration_minutes=60,
                exam_type="mid"), clock["now"])
            result = exams.schedule(s, runtime, faculty, exam.id, clock["now"])
        eid, exam_mission = result.exam.id, result.guardian_mission_id
        trace("exam_scheduled", exam_id=eid, target_count=result.target_count, guardian_mission_id=exam_mission)
        work("worker_at_scheduling")

        at(EXAM_START - timedelta(hours=24))
        work("worker_reminder_checkpoint_24h")
        reminders = requests(exam_id=eid, purpose="exam_reminder")
        trace("reminders_requested", count=len(reminders), mission=mission_state(exam_mission))

        def mark_exam(marks: dict) -> None:
            with factory.open_tenant_session(org) as s:
                out = exams.mark_attendance(s, faculty, eid, exams.MarkExamAttendance(marks=[
                    exams.AttendanceMarkIn(student_code=c, status=v) for c, v in marks.items()]), clock["now"])
            trace("exam_attendance_marked", changed=out.changed, statuses={ids[c]: v.value for c, v in marks.items()})

        at(EXAM_START - timedelta(minutes=5))
        with factory.open_tenant_session(org) as s:
            exams.start(s, faculty, eid, clock["now"])
        at(EXAM_START + timedelta(minutes=5))
        mark_exam({CODES[0]: ExamAttendanceStatus.PRESENT, CODES[1]: ExamAttendanceStatus.PRESENT,
                   CODES[2]: ExamAttendanceStatus.ABSENT})
        work("worker_during_grace")
        at(EXAM_START + timedelta(minutes=15))
        work("worker_after_grace")
        absence = requests(exam_id=eid, purpose="absence_followup")
        trace("absence_followup_requested", count=len(absence), student_ids=[p["student_id"] for p in absence],
              mission=mission_state(exam_mission))

        at(EXAM_START + timedelta(minutes=70))
        with factory.open_tenant_session(org) as s:
            exams.complete(s, faculty, eid, clock["now"])
        at(EXAM_START + timedelta(days=1))
        mark_exam({CODES[2]: ExamAttendanceStatus.MAKEUP_COMPLETED})
        work("worker_after_makeup")
        exam_final = mission_state(exam_mission)
        trace("exam_final", mission=exam_final)

        # --- Attendance Guardian -----------------------------------------------------------------------------------
        with factory.open_tenant_session(org) as s:
            teaching = s.get(TeachingAssignment, teaching_id)
            row = AttendanceSession(teaching_assignment_id=teaching_id, course_id=teaching.course_id,
                                    faculty_id=teaching.faculty_id, session_date=local(CLASS_START).date(),
                                    scheduled_start=CLASS_START, scheduled_end=CLASS_START + timedelta(hours=1),
                                    room="E-101", status=AttendanceSessionStatus.SCHEDULED)
            s.add(row)
            s.commit()
            session_id = row.id
        at(CLASS_START)

        def class_op(operation, *args) -> None:
            with factory.open_tenant_session(org) as s:
                profile = s.get(FacultyProfile, faculty.faculty_profile_id)
                account = s.get(AuthAccount, faculty.account_id)
                operation(s, profile, account, session_id, *args, clock["now"])

        class_op(faculty_ops.start_class)
        trace("class_started", session_id=session_id)
        at(CLASS_START + timedelta(minutes=1))
        class_op(faculty_ops.mark_attendance, {CODES[0]: "present", CODES[1]: "present"})
        trace("class_attendance_marked", present=[ids[CODES[0]], ids[CODES[1]]], missing=[ids[CODES[2]]])
        at(CLASS_START + timedelta(minutes=16))
        scan = scan_attendance(factory, runtime, now=clock["now"])
        trace("absence_scan", **scan.model_dump())
        with factory.open_tenant_session(org) as s:
            case = s.execute(select(AttendanceIntervention)).scalars().one()
            case_id, case_mission = case.id, case.mission_id
        trace("intervention_created", intervention_id=case_id, student_id=ids[CODES[2]], mission_id=case_mission)
        work("worker_attendance_case")
        attendance_requests = requests(intervention_id=case_id)
        trace("attendance_followup_requested", count=len(attendance_requests), mission=mission_state(case_mission))

        at(CLASS_START + timedelta(minutes=30))
        class_op(faculty_ops.mark_attendance, {CODES[2]: "present"})
        trace("attendance_corrected", student_id=ids[CODES[2]], status="present")
        work("worker_after_correction")
        with factory.open_tenant_session(org) as s:
            case = s.get(AttendanceIntervention, case_id)
            resolution = (case.status.value, case.resolution_code)
        attendance_final = mission_state(case_mission)
        trace("attendance_final", intervention_status=resolution[0], resolution_code=resolution[1],
              mission=attendance_final)
        engine.dispose()

    ok = (len(reminders) == 3 and len(absence) == 1 and absence[0]["student_id"] == ids[CODES[2]]
          and exam_final["status"] == "completed" and (exam_final["outcome"] or {}).get("result") == "ALL_RESOLVED"
          and scan.interventions_created == 1 and len(attendance_requests) == 1
          and attendance_final["status"] == "completed" and resolution == ("resolved", "ATTENDANCE_RECORDED"))
    trace("result", ok=ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
