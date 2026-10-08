"""CAMPUS AI demo enrichment: a coherent, interconnected campus on top of ``scripts/seed_data.py``.

``run_seed`` (the shared base seed, also used by the test suite) creates departments, students, faculty,
courses, enrollments, attendance counters, the weekly timetable, timetable-only exams, career, events, cases
and the development accounts. This script adds what the CAMPUS AI demo needs so that every ordinary question
has a real answer in the database:

* extra weekly timetable slots, so Aditi's CSE semester-5 section has classes every day Monday-Saturday;
* three weeks of held (CLOSED) class sessions with per-student marks for the four semester-5 CSE classes.
  The seeded ``attendance_records`` counters already cover the whole semester, so these recent sessions are
  their detail and are NOT folded into the counters again;
* assignments (closed, live with a Guardian, and upcoming), their roster snapshot and submissions;
* managed exams (one completed quiz with attendance, two scheduled with a Guardian) and their targets;
* communication preferences, assignment / exam notifications and one more internship application.

Live items (open assignments, scheduled exams) go through the real application services
(``app.services.assignments`` / ``app.services.exams``), so each gets its Guardian mission exactly as in
production. Historical items (already closed / completed) cannot be created by those services (their dates are
in the past), so they are inserted directly with their final status and no Guardian.

Everything is relative to ``now`` (default: the current time), deterministic for a given ``now`` and idempotent:
every row is looked up by its natural key first, so a second run adds nothing.

Safety: this only ever writes to a SQLite database. ``run_demo_enrichment`` refuses any other dialect, and the
command line opens an explicit SQLite path (default ``data/demo/campusnexus_demo.db``), never
``CAMPUSNEXUS_DATABASE_URL``, so it cannot reach a PostgreSQL/Supabase database by accident.

Usage:
    python scripts/seed_demo_campus.py                 # the demo database (after scripts/reset_demo_env.py)
    python scripts/seed_demo_campus.py --db path.db    # another local SQLite file
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.agentos.runtime import MissionActor  # noqa: E402
from app.db.models.academic import AttendanceRecord, Course, Enrollment, Exam, ExamStatus, ExamType, TimetableSlot  # noqa: E402
from app.db.models.assignment import Assignment, AssignmentStatus, AssignmentSubmission, AssignmentTarget, SubmissionStatus  # noqa: E402
from app.db.models.auth import AuthAccount  # noqa: E402
from app.db.models.career import Application, ApplicationStatus, Opportunity  # noqa: E402
from app.db.models.communication import Notification, NotificationStatus  # noqa: E402
from app.db.models.communication_delivery import CommunicationPreference  # noqa: E402
from app.db.models.exam import ExamAttendance, ExamAttendanceStatus, ExamTarget  # noqa: E402
from app.db.models.faculty import (  # noqa: E402
    AttendanceMarkStatus, AttendanceSession, AttendanceSessionStatus, SessionAttendanceMark, TeachingAssignment,
)
from app.db.models.identity import Student  # noqa: E402
from app.db.tenancy import DEFAULT_ORGANIZATION_SLUG  # noqa: E402
from app.db.models.organization import Organization  # noqa: E402
from app.db.tenant_session import TenantSessionFactory  # noqa: E402
from app.schemas.enums import UserRole  # noqa: E402
from app.services.class_schedule import roster  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DEMO_DB_PATH = REPO_ROOT / "data" / "demo" / "campusnexus_demo.db"
SECTION = "1"
HISTORY_DAYS = 21
DEMO_STUDENT_CODE = "STU-DEMO-001"

# Extra weekly slots: (course, weekday 0=Mon, start, end, room). Chosen so the event scenarios of the base seed keep
# their meaning (AI workshop Saturday 14:00 conflict-free, Competitive Coding Contest 15:00-18:00 conflict-free).
EXTRA_TIMETABLE: List[Tuple[str, int, time, time, str]] = [
    ("CS304", 0, time(13, 30), time(14, 30), "Block A - Room 207"),
    ("CS302", 1, time(13, 30), time(14, 30), "DB Lab - Block A Room 110"),
    ("CS301", 2, time(11, 15), time(12, 15), "Block A - Room 204"),
    ("CS302", 3, time(11, 15), time(12, 15), "Block A - Room 205"),
    ("CS303", 4, time(11, 15), time(12, 15), "Block A - Room 206"),
    ("CS301", 4, time(13, 30), time(14, 30), "Systems Lab - Block A Room 112"),
    ("CS303", 5, time(9, 0), time(10, 30), "Networks Lab - Block A Room 114"),
    ("CS304", 5, time(10, 45), time(11, 45), "Block A - Room 207"),
]
SEMESTER5_COURSES = ("CS301", "CS302", "CS303", "CS304")


@dataclass(frozen=True)
class AssignmentSeed:
    course: str
    title: str
    description: str
    published_days_ago: float
    deadline_days: float  # relative to now; negative = already passed (closed)
    submitted: Dict[str, str]  # student_code -> "on_time" | "late"


ASSIGNMENT_SEED: List[AssignmentSeed] = [
    AssignmentSeed(
        "CS303", "Lab 3: Subnetting and CIDR Worksheet", "Solve the 12 subnetting problems and submit the worksheet.",
        16, -9, {code: "on_time" for code in ("STU-DEMO-001", "STU2023002", "STU2023003", "STU2023007", "STU2023021",
                                               "STU2023022", "STU2023024", "STU2023026", "STU2023027", "STU2023028")}
        | {"STU2023025": "late"}),
    AssignmentSeed(
        "CS302", "DBMS Mini-Project Phase 1: ER Model", "Submit the ER diagram and relational schema for your project.",
        10, -2, {code: "on_time" for code in ("STU-DEMO-001", "STU2023002", "STU2023007", "STU2023021", "STU2023022",
                                               "STU2023024", "STU2023026", "STU2023028")}
        | {"STU2023003": "late", "STU2023027": "late"}),
    AssignmentSeed(
        "CS301", "OS Assignment 2: Process Scheduling Simulation",
        "Implement FCFS, SJF and Round Robin and compare average waiting time.",
        5, 5, {code: "on_time" for code in ("STU-DEMO-001", "STU2023002", "STU2023007", "STU2023022", "STU2023026")}),
    AssignmentSeed(
        "CS303", "Lab 4: Socket Programming Report", "TCP echo server and client in Python, with a short report.",
        3, 3, {code: "on_time" for code in ("STU2023002", "STU2023007", "STU2023021", "STU2023022", "STU2023024",
                                             "STU2023026", "STU2023028")}),
    AssignmentSeed(
        "CS304", "SE Case Study: Requirements Specification",
        "Write the SRS for the campus library system case study (IEEE 830 outline).",
        1, 6, {"STU2023007": "on_time"}),
]


@dataclass(frozen=True)
class ExamSeed:
    course: str
    title: str
    exam_type: ExamType
    day_offset: int  # relative to today; negative = already held
    start: time
    minutes: int
    location: str
    absent: Tuple[str, ...] = field(default_factory=tuple)


EXAM_SEED: List[ExamSeed] = [
    ExamSeed("CS303", "Quiz 1: Physical and Data Link Layers", ExamType.QUIZ, -12, time(16, 0), 45,
             "Block A - Room 206", absent=("STU2023023",)),
    ExamSeed("CS303", "Quiz 2: Transport Layer", ExamType.QUIZ, 2, time(16, 0), 45, "Block A - Room 206"),
    ExamSeed("CS301", "Internal Assessment 1: Processes and Scheduling", ExamType.INTERNAL, 6, time(14, 0), 90,
             "Exam Hall 2"),
]


@dataclass
class EnrichmentSummary:
    timetable_slots: int = 0
    attendance_sessions: int = 0
    attendance_marks: int = 0
    assignments: int = 0
    assignment_submissions: int = 0
    exams: int = 0
    exam_attendance: int = 0
    communication_preferences: int = 0
    notifications: int = 0
    applications: int = 0
    guardian_missions: int = 0


class DemoSeedRefused(RuntimeError):
    """The target database is not a local SQLite demo database."""


# ---------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------


def require_local_sqlite(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise DemoSeedRefused(f"The CAMPUS AI demo seed only writes to a local SQLite database, not {engine.dialect.name}.")


def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=IST).astimezone(timezone.utc)


def _teaching(session: Session, course_code: str) -> TeachingAssignment:
    teaching = session.execute(
        select(TeachingAssignment).join(Course, Course.id == TeachingAssignment.course_id)
        .where(Course.code == course_code, TeachingAssignment.section == SECTION)
    ).scalar_one_or_none()
    if teaching is None:
        raise SystemExit(f"No teaching assignment for {course_code} section {SECTION}: run scripts/seed_data.py first.")
    return teaching


def _faculty_account(session: Session, faculty_profile_id: int) -> AuthAccount:
    account = session.execute(
        select(AuthAccount).where(AuthAccount.linked_faculty_id == faculty_profile_id).order_by(AuthAccount.id)
    ).scalars().first()
    if account is None:
        raise SystemExit("The faculty development accounts are missing: run seed_dev_accounts (reset_demo_env.py) first.")
    return account


def _student_account(session: Session, student_code: str) -> Optional[AuthAccount]:
    return session.execute(select(AuthAccount).where(AuthAccount.linked_student_id == student_code)).scalars().first()


def _actor(session: Session, organization_id: int, teaching: TeachingAssignment) -> MissionActor:
    account = _faculty_account(session, teaching.faculty_id)
    return MissionActor(organization_id=organization_id, account_id=account.id, role=UserRole.FACULTY,
                        faculty_profile_id=teaching.faculty_id)


def _attendance_ratio(session: Session, student: Student, course_id: int) -> float:
    row = session.execute(
        select(AttendanceRecord.classes_attended, AttendanceRecord.classes_conducted)
        .join(Enrollment, Enrollment.id == AttendanceRecord.enrollment_id)
        .where(Enrollment.student_id == student.id, Enrollment.course_id == course_id)
    ).first()
    if row is None or not row[1]:
        return 1.0
    return row[0] / row[1]


def _mark(rate_absent: float, session_index: int, student_index: int) -> AttendanceMarkStatus:
    """Evenly spread absences matching the student's semester absence rate; an occasional late arrival."""
    shifted = session_index + student_index
    if int((shifted + 1) * rate_absent) > int(shifted * rate_absent):
        return AttendanceMarkStatus.ABSENT
    if (session_index * 3 + student_index) % 11 == 5:
        return AttendanceMarkStatus.LATE
    return AttendanceMarkStatus.PRESENT


# ---------------------------------------------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------------------------------------------


def seed_timetable(session: Session, summary: EnrichmentSummary) -> None:
    for code, weekday, start, end, room in EXTRA_TIMETABLE:
        course = session.execute(select(Course).where(Course.code == code)).scalar_one()
        exists = session.execute(select(TimetableSlot.id).where(
            TimetableSlot.course_id == course.id, TimetableSlot.weekday == weekday, TimetableSlot.start_time == start)).first()
        if exists is None:
            session.add(TimetableSlot(course_id=course.id, weekday=weekday, start_time=start, end_time=end,
                                      location=room, semester=course.semester))
            summary.timetable_slots += 1
    session.flush()


def seed_attendance_history(session: Session, now: datetime, summary: EnrichmentSummary) -> None:
    today = now.astimezone(IST).date()
    for code in SEMESTER5_COURSES:
        teaching = _teaching(session, code)
        students = roster(session, teaching)
        ratios = {s.id: _attendance_ratio(session, s, teaching.course_id) for s in students}
        slots = session.execute(select(TimetableSlot).where(TimetableSlot.course_id == teaching.course_id)
                                .order_by(TimetableSlot.weekday, TimetableSlot.start_time)).scalars().all()
        meetings: List[Tuple[datetime, datetime, TimetableSlot]] = []
        for offset in range(HISTORY_DAYS, -1, -1):
            day = today - timedelta(days=offset)
            for slot in slots:
                if slot.weekday != day.weekday():
                    continue
                start, end = _at(day, slot.start_time), _at(day, slot.end_time)
                if end < now:  # only meetings that are over
                    meetings.append((start, end, slot))
        for index, (start, end, slot) in enumerate(meetings):
            row = session.execute(select(AttendanceSession).where(
                AttendanceSession.teaching_assignment_id == teaching.id,
                AttendanceSession.scheduled_start == start)).scalar_one_or_none()
            if row is not None:
                continue
            row = AttendanceSession(
                teaching_assignment_id=teaching.id, course_id=teaching.course_id, faculty_id=teaching.faculty_id,
                timetable_slot_id=slot.id, session_date=start.astimezone(IST).date(), scheduled_start=start,
                scheduled_end=end, room=slot.location, status=AttendanceSessionStatus.CLOSED,
                actual_started_at=start + timedelta(minutes=2), actual_closed_at=end,
            )
            session.add(row)
            session.flush()
            summary.attendance_sessions += 1
            for student_index, student in enumerate(students):
                status = _mark(1.0 - ratios[student.id], index, student_index)
                session.add(SessionAttendanceMark(session_id=row.id, student_id=student.id, status=status,
                                                  marked_at=start + timedelta(minutes=10),
                                                  marked_by_faculty_id=teaching.faculty_id))
                summary.attendance_marks += 1
    session.flush()


def _students_by_code(session: Session, students: List[Student]) -> Dict[str, Student]:
    return {s.student_code: s for s in students}


def _add_submissions(session: Session, assignment: Assignment, spec: AssignmentSeed, students: Dict[str, Student],
                     recorder_account_id: int, now: datetime, summary: EnrichmentSummary) -> None:
    """Submissions: a student's own account when it exists, else recorded by the course faculty (hard copy)."""
    published = assignment.published_at or now
    for index, (code, timing) in enumerate(sorted(spec.submitted.items())):
        student = students.get(code)
        if student is None:
            continue
        exists = session.execute(select(AssignmentSubmission.id).where(
            AssignmentSubmission.assignment_id == assignment.id, AssignmentSubmission.student_id == student.id)).first()
        if exists is not None:
            continue
        if timing == "late":
            submitted_at = assignment.deadline_at + timedelta(hours=6 + index)
            status = SubmissionStatus.LATE
        else:
            latest = min(assignment.deadline_at, now) - timedelta(hours=1)
            submitted_at = max(published + timedelta(hours=2), latest - timedelta(hours=5 * (index + 1)))
            status = SubmissionStatus.SUBMITTED
        account = _student_account(session, code)
        session.add(AssignmentSubmission(
            assignment_id=assignment.id, student_id=student.id, submitted_at=submitted_at, status=status,
            submitted_by_account_id=account.id if account is not None else recorder_account_id))
        summary.assignment_submissions += 1
    session.flush()


def seed_assignments(session: Session, runtime: object, organization_id: int, now: datetime,
                     summary: EnrichmentSummary) -> None:
    from app.services import assignments as service

    for spec in ASSIGNMENT_SEED:
        teaching = _teaching(session, spec.course)
        actor = _actor(session, organization_id, teaching)
        students = roster(session, teaching)
        assignment = session.execute(select(Assignment).where(
            Assignment.teaching_assignment_id == teaching.id, Assignment.title == spec.title)).scalar_one_or_none()
        published_at = now - timedelta(days=spec.published_days_ago)
        deadline = (now + timedelta(days=spec.deadline_days)).astimezone(IST).replace(hour=23, minute=59, second=0,
                                                                                       microsecond=0)
        if assignment is None and spec.deadline_days > 0:
            # Live: the real create + publish path (roster snapshot, Guardian mission, events, audit).
            created = service.create_assignment(session, actor, service.CreateAssignment(
                teaching_assignment_id=teaching.id, title=spec.title, description=spec.description,
                deadline_at=deadline), published_at)
            result = service.publish(session, runtime, actor, created.id, published_at)
            assignment = session.get(Assignment, created.id)
            summary.assignments += 1
            summary.guardian_missions += 1 if result.created else 0
        elif assignment is None:
            # Historical: already closed, so no Guardian; the roster snapshot is taken as publishing would.
            assignment = Assignment(
                created_by_account_id=actor.account_id, created_by_faculty_id=teaching.faculty_id,
                teaching_assignment_id=teaching.id, course_id=teaching.course_id, title=spec.title,
                description=spec.description, status=AssignmentStatus.CLOSED, published_at=published_at,
                deadline_at=deadline, created_at=published_at, updated_at=deadline)
            session.add(assignment)
            session.flush()
            session.add_all([AssignmentTarget(assignment_id=assignment.id, student_id=s.id, student_code=s.student_code,
                                              section=s.section, created_at=published_at) for s in students])
            session.flush()
            summary.assignments += 1
        _add_submissions(session, assignment, spec, _students_by_code(session, students), actor.account_id, now, summary)
    session.flush()


def seed_exams(session: Session, runtime: object, organization_id: int, now: datetime,
               summary: EnrichmentSummary) -> None:
    from app.services import exams as service

    today = now.astimezone(IST).date()
    for spec in EXAM_SEED:
        teaching = _teaching(session, spec.course)
        actor = _actor(session, organization_id, teaching)
        start = _at(today + timedelta(days=spec.day_offset), spec.start)
        end = start + timedelta(minutes=spec.minutes)
        exam = session.execute(select(Exam).where(
            Exam.teaching_assignment_id == teaching.id, Exam.title == spec.title)).scalar_one_or_none()
        if exam is not None:
            continue
        if spec.day_offset > 0:
            announced = now - timedelta(days=2)
            created = service.create_exam(session, actor, service.CreateExam(
                teaching_assignment_id=teaching.id, title=spec.title, scheduled_at=start,
                duration_minutes=spec.minutes, exam_type=spec.exam_type, location=spec.location), announced)
            result = service.schedule(session, runtime, actor, created.id, announced)
            summary.exams += 1
            summary.guardian_missions += 1 if result.created else 0
            continue
        students = roster(session, teaching)
        exam = Exam(
            course_id=teaching.course_id, teaching_assignment_id=teaching.id, created_by_account_id=actor.account_id,
            title=spec.title, exam_type=spec.exam_type.value, scheduled_start=start, scheduled_end=end,
            duration_minutes=spec.minutes, location=spec.location, status=ExamStatus.COMPLETED,
            published_at=start - timedelta(days=5), started_at=start, completed_at=end,
            created_at=start - timedelta(days=5), updated_at=end)
        session.add(exam)
        session.flush()
        for student in students:
            session.add(ExamTarget(exam_id=exam.id, student_id=student.id, student_code=student.student_code,
                                   section=student.section, created_at=exam.published_at))
            session.add(ExamAttendance(
                exam_id=exam.id, student_id=student.id, marked_at=start + timedelta(minutes=5),
                marked_by_account_id=actor.account_id,
                status=ExamAttendanceStatus.ABSENT if student.student_code in spec.absent else ExamAttendanceStatus.PRESENT))
            summary.exam_attendance += 1
        summary.exams += 1
    session.flush()


def seed_preferences_and_notifications(session: Session, now: datetime, summary: EnrichmentSummary) -> None:
    teaching = _teaching(session, "CS303")
    for index, student in enumerate(roster(session, teaching)):
        exists = session.execute(select(CommunicationPreference.id).where(
            CommunicationPreference.student_id == student.id)).first()
        if exists is not None:
            continue
        demo = student.student_code == DEMO_STUDENT_CODE
        session.add(CommunicationPreference(
            student_id=student.id, allow_in_app=True, allow_email=True, allow_voice=demo or index % 3 == 0,
            allow_sms=False, allow_whatsapp=False, quiet_start="22:00", quiet_end="07:00",
            preferred_channel="in_app" if demo or index % 2 else "email"))
        summary.communication_preferences += 1

    demo = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT_CODE)).scalar_one()
    notices = [
        ("New assignment: Lab 4 Socket Programming Report", "Computer Networks (CS303) Lab 4 is due in 3 days.", "academic"),
        ("Quiz scheduled: CS303 Quiz 2", "Quiz 2: Transport Layer is scheduled in 2 days, Block A - Room 206.", "academic"),
        ("New assignment: SE Requirements Specification", "Software Engineering (CS304) case study is due in 6 days.",
         "academic"),
    ]
    for title, body, category in notices:
        exists = session.execute(select(Notification.id).where(
            Notification.student_id == demo.id, Notification.title == title)).first()
        if exists is None:
            session.add(Notification(student_id=demo.id, title=title, body=body, category=category,
                                     status=NotificationStatus.SENT, sent_at=now, created_at=now))
            summary.notifications += 1

    opportunity = session.execute(select(Opportunity).where(Opportunity.title == "Full-Stack Developer Intern")).scalar_one_or_none()
    if opportunity is not None:
        exists = session.execute(select(Application.id).where(
            Application.student_id == demo.id, Application.opportunity_id == opportunity.id)).first()
        if exists is None:
            session.add(Application(student_id=demo.id, opportunity_id=opportunity.id,
                                    status=ApplicationStatus.SUBMITTED, applied_at=now - timedelta(days=4)))
            summary.applications += 1
    session.flush()


# ---------------------------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------------------------


def run_demo_enrichment(engine: Engine, *, now: Optional[datetime] = None) -> EnrichmentSummary:
    """Enrich an already seeded (``run_seed`` + ``seed_dev_accounts``) local SQLite database. Idempotent."""
    from app.agentos.bootstrap import build_agent_runtime
    from app.agentos.providers import MockAgentBrain

    require_local_sqlite(engine)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    factory = TenantSessionFactory(engine)
    with factory() as probe:
        organization = probe.execute(select(Organization).where(Organization.slug == DEFAULT_ORGANIZATION_SLUG)).scalar_one_or_none()
        if organization is None:
            raise SystemExit("The demo organization is missing: run scripts/seed_data.py first.")
        organization_id = organization.id
    runtime = build_agent_runtime(MockAgentBrain(), clock=lambda: now)
    summary = EnrichmentSummary()
    with factory.open_tenant_session(organization_id) as session:
        seed_timetable(session, summary)
        seed_attendance_history(session, now, summary)
        session.commit()
        seed_assignments(session, runtime, organization_id, now, summary)
        session.commit()
        seed_exams(session, runtime, organization_id, now, summary)
        session.commit()
        seed_preferences_and_notifications(session, now, summary)
        session.commit()
    return summary


def main() -> None:
    from app.db.session import open_database

    parser = argparse.ArgumentParser(description="Enrich the local SQLite demo database for CAMPUS AI.")
    parser.add_argument("--db", default=str(DEMO_DB_PATH), help="SQLite database file (default: the demo database).")
    args = parser.parse_args()
    path = Path(args.db).resolve()
    if not path.exists():
        raise SystemExit(f"{path} does not exist. Run scripts/reset_demo_env.py first.")
    engine = open_database(str(path))  # an explicit path is always SQLite (and is upgraded additively)
    try:
        summary = run_demo_enrichment(engine)
    finally:
        engine.dispose()
    print("CAMPUS AI demo enrichment (rows added):")
    for name, value in summary.__dict__.items():
        print(f"  {name}: {value}")


if __name__ == "__main__":
    main()
