"""CAMPUS AI demo scenarios: arm the real autonomous flows so they happen live during a demo. Local SQLite only.

These helpers only set up *inputs* through the same application services production uses; every outcome (Guardian
wake-ups, follow-up requests, communication jobs, in-app notifications, phone calls) is then produced by the running
worker (``python scripts/process_due_missions.py --loop --interval 15``). Nothing here writes a result.

    python scripts/demo_scenarios.py exam-reminder [--in-minutes 3]
        Schedules a new managed exam for the demo class (CS303 section 1, Dr. Ashok Verma) through
        ``app.services.exams``, starting ``max(reminder hours) + N minutes`` from now. The Exam Guardian's first
        reminder checkpoint (start - 24 h with the production policy) therefore falls due N minutes from now: the
        Guardian waits, wakes on its own at the checkpoint, requests a reminder, and the Communication Agent delivers
        it in-app. Production checkpoint timing is not changed.

    python scripts/demo_scenarios.py absence [--start-and-mark]
        Records an extra CS303 class starting now (``schedule_demo_class``). With ``--start-and-mark`` it also starts
        the class and marks every student PRESENT except the designated demo student (ABSENT), through the faculty
        operations service (the same path as the faculty UI). After the absence grace period
        (``CAMPUSNEXUS_ATTENDANCE_ABSENCE_GRACE_MINUTES``; short values only with ``CAMPUSNEXUS_DEMO_MODE=1``) the worker
        detects the absence and the Attendance Guardian takes over.

    python scripts/demo_scenarios.py voice-preference [--off]
        Makes the designated demo student prefer voice calls (consent on, preferred channel ``voice``), or back to
        in-app with ``--off``. A call is only ever placed when the Exotel transport is configured; otherwise the
        policy never offers voice. The phone number is never stored: in demo mode ``ContactResolver`` maps this one
        student to ``CAMPUSNEXUS_TEST_PHONE`` in memory at delivery time.

Database: ``--db`` (default: ``CAMPUSNEXUS_DB_PATH`` or ``data/demo/campusnexus_demo.db``). Refuses anything but SQLite.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app.db.models.academic import Course, ExamType  # noqa: E402
from app.db.models.auth import AuthAccount  # noqa: E402
from app.db.models.communication_delivery import CommunicationPreference  # noqa: E402
from app.db.models.faculty import AttendanceMarkStatus, FacultyProfile, TeachingAssignment  # noqa: E402
from app.db.models.identity import Student  # noqa: E402
from app.db.models.organization import Organization  # noqa: E402
from app.db.tenancy import DEFAULT_ORGANIZATION_SLUG  # noqa: E402
from app.db.tenant_session import TenantSessionFactory  # noqa: E402
from scripts.seed_demo_campus import DEMO_STUDENT_CODE, SECTION, require_local_sqlite  # noqa: E402

DEMO_DB = REPO_ROOT / "data" / "demo" / "campusnexus_demo.db"
COURSE = "CS303"


def _organization_id(factory: TenantSessionFactory) -> int:
    with factory() as probe:
        org = probe.execute(select(Organization.id).where(Organization.slug == DEFAULT_ORGANIZATION_SLUG)).scalar_one_or_none()
    if org is None:
        raise SystemExit("The demo organization is missing: run scripts/reset_demo_env.py first.")
    return org


def _teaching(session) -> TeachingAssignment:
    teaching = session.execute(select(TeachingAssignment).join(Course, Course.id == TeachingAssignment.course_id)
                               .where(Course.code == COURSE, TeachingAssignment.section == SECTION)).scalar_one_or_none()
    if teaching is None:
        raise SystemExit(f"No {COURSE} section {SECTION} class: run scripts/reset_demo_env.py first.")
    return teaching


def _faculty(session, teaching: TeachingAssignment):
    account = session.execute(select(AuthAccount).where(AuthAccount.linked_faculty_id == teaching.faculty_id)
                              .order_by(AuthAccount.id)).scalars().first()
    if account is None:
        raise SystemExit("The faculty development account is missing: run scripts/reset_demo_env.py first.")
    return session.get(FacultyProfile, teaching.faculty_id), account


def designated_student_code() -> str:
    return (os.environ.get("CAMPUSNEXUS_DEMO_CALL_STUDENT") or DEMO_STUDENT_CODE).strip()


def arm_exam_reminder(engine: Engine, *, in_minutes: float = 3, now: Optional[datetime] = None) -> dict:
    """Schedule one managed exam whose first reminder checkpoint is ``in_minutes`` from ``now``."""
    from app.agentos.bootstrap import build_agent_runtime
    from app.agentos.exam_guardian import policy_from_env
    from app.agentos.providers import MockAgentBrain
    from app.agentos.runtime import MissionActor
    from app.schemas.enums import UserRole
    from app.services import exams as service

    require_local_sqlite(engine)
    if not 1 <= in_minutes <= 60:
        raise SystemExit("--in-minutes must be 1-60")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    policy = policy_from_env()
    lead = timedelta(hours=max(policy.reminder_hours))
    start = (now + lead + timedelta(minutes=in_minutes)).replace(second=0, microsecond=0) + timedelta(minutes=1)
    factory = TenantSessionFactory(engine)
    organization_id = _organization_id(factory)
    # The runtime only creates the Guardian mission here; the worker's configured brain advances it later.
    runtime = build_agent_runtime(MockAgentBrain(), clock=lambda: now)
    with factory.open_tenant_session(organization_id) as session:
        teaching = _teaching(session)
        _, account = _faculty(session, teaching)
        actor = MissionActor(organization_id=organization_id, account_id=account.id, role=UserRole.FACULTY,
                             faculty_profile_id=teaching.faculty_id)
        title = f"Demo Quiz (reminder at {(start - lead).astimezone(_ist()).strftime('%H:%M')} IST)"
        exam = service.create_exam(session, actor, service.CreateExam(
            teaching_assignment_id=teaching.id, title=title, scheduled_at=start, duration_minutes=30,
            exam_type=ExamType.QUIZ, location="Block A - Room 206"), now)
        result = service.schedule(session, runtime, actor, exam.id, now)
    return {"scenario": "exam_reminder", "exam_id": exam.id, "guardian_mission_id": result.guardian_mission_id,
            "exam_starts_at": start.isoformat(), "reminder_checkpoint_at": (start - lead).isoformat(),
            "reminder_hours": list(policy.reminder_hours)}


def arm_absence(engine: Engine, *, start_and_mark: bool, now: Optional[datetime] = None) -> dict:
    """An extra class now; optionally started and marked with the designated student absent."""
    from app.services import faculty_ops
    from scripts.schedule_demo_class import schedule_extra_class

    require_local_sqlite(engine)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    factory = TenantSessionFactory(engine)
    organization_id = _organization_id(factory)
    code = designated_student_code()
    with factory.open_tenant_session(organization_id) as session:
        row = schedule_extra_class(session, course_code=COURSE, section=SECTION, minutes=60, now=now)
        out = {"scenario": "absence", "class_session_id": row.id, "scheduled_start": row.scheduled_start.isoformat(),
               "absent_student": code, "started": False}
        if not start_and_mark:
            return out
        teaching = session.get(TeachingAssignment, row.teaching_assignment_id)
        faculty, account = _faculty(session, teaching)
        from app.services.faculty_ops import roster

        codes = [s.student_code for s in roster(session, teaching)]
        if code not in codes:
            raise SystemExit(f"{code} is not on the {COURSE} roster.")
        if row.status.value == "scheduled":
            faculty_ops.start_class(session, faculty, account, row.id, now)
        marks = {c: (AttendanceMarkStatus.ABSENT if c == code else AttendanceMarkStatus.PRESENT).value for c in codes}
        faculty_ops.mark_attendance(session, faculty, account, row.id, marks, now)
        out.update(started=True, marked=len(marks))
        return out


def set_voice_preference(engine: Engine, *, enabled: bool) -> dict:
    require_local_sqlite(engine)
    factory = TenantSessionFactory(engine)
    organization_id = _organization_id(factory)
    code = designated_student_code()
    with factory.open_tenant_session(organization_id) as session:
        student = session.execute(select(Student).where(Student.student_code == code)).scalar_one_or_none()
        if student is None:
            raise SystemExit(f"{code} not found.")
        row = session.execute(select(CommunicationPreference).where(
            CommunicationPreference.student_id == student.id)).scalars().first()
        if row is None:
            row = CommunicationPreference(student_id=student.id, allow_in_app=True, allow_email=True, allow_sms=False,
                                          allow_whatsapp=False, quiet_start="22:00", quiet_end="07:00")
            session.add(row)
        row.allow_voice = enabled
        row.preferred_channel = "voice" if enabled else "in_app"
        session.commit()
    return {"scenario": "voice_preference", "student": code, "allow_voice": enabled,
            "preferred_channel": "voice" if enabled else "in_app"}


def _ist():
    return timezone(timedelta(hours=5, minutes=30))


def main(argv: Optional[list] = None) -> int:
    from app.db.session import open_database

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("CAMPUSNEXUS_DB_PATH") or str(DEMO_DB))
    sub = parser.add_subparsers(dest="scenario", required=True)
    exam = sub.add_parser("exam-reminder")
    exam.add_argument("--in-minutes", type=float, default=3)
    absence = sub.add_parser("absence")
    absence.add_argument("--start-and-mark", action="store_true")
    voice = sub.add_parser("voice-preference")
    voice.add_argument("--off", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.db).resolve()
    if not path.exists():
        raise SystemExit(f"{path} does not exist. Run scripts/reset_demo_env.py first.")
    engine = open_database(str(path))
    try:
        if args.scenario == "exam-reminder":
            result = arm_exam_reminder(engine, in_minutes=args.in_minutes)
        elif args.scenario == "absence":
            result = arm_absence(engine, start_and_mark=args.start_and_mark)
        else:
            result = set_voice_preference(engine, enabled=not args.off)
    finally:
        engine.dispose()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
