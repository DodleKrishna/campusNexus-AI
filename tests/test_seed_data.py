"""Tests for scripts/seed_data.py: idempotency and required demo scenarios."""
from __future__ import annotations

from sqlalchemy import func, select

from app.db.base import utc_now
from app.db.models.career import Application, Opportunity, OpportunityStatus
from app.db.models.events import Event, EventRegistration, EventStatus
from app.db.models.identity import Department, Student
from app.db.models.academic import AttendanceRecord, Course, Enrollment, Exam
from app.db.models.services import CampusCase, CaseSLA, CaseStatus
from scripts.seed_data import DEMO_STUDENT_CODE, build_summary, run_seed


def _row_count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_seed_is_idempotent(session_factory) -> None:
    with session_factory() as s1:
        summary_first = run_seed(s1)
    with session_factory() as s2:
        summary_second = run_seed(s2)

    assert summary_first == summary_second


def test_seed_running_twice_does_not_duplicate_demo_student(session_factory) -> None:
    with session_factory() as s1:
        run_seed(s1)
    with session_factory() as s2:
        run_seed(s2)
    with session_factory() as s3:
        count = s3.execute(
            select(func.count()).select_from(Student).where(Student.student_code == DEMO_STUDENT_CODE)
        ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# Core reference data
# ---------------------------------------------------------------------------


def test_expected_departments_exist(seeded_session) -> None:
    codes = {d.code for d in seeded_session.execute(select(Department)).scalars().all()}
    assert {"CSE", "ECE", "MECH", "CIVIL"}.issubset(codes)


def test_expected_courses_exist(seeded_session) -> None:
    codes = {c.code for c in seeded_session.execute(select(Course)).scalars().all()}
    assert {"CS301", "CS302", "CS303", "CS304"}.issubset(codes)


def test_demo_student_exists_with_expected_profile(seeded_session) -> None:
    student = seeded_session.execute(
        select(Student).where(Student.student_code == DEMO_STUDENT_CODE)
    ).scalar_one()
    assert student.department.code == "CSE"
    assert student.year == 3
    assert student.semester == 5
    assert student.cgpa == 7.8
    assert student.user.full_name


# ---------------------------------------------------------------------------
# Attendance shortage scenario (13.A)
# ---------------------------------------------------------------------------


def test_attendance_raw_counts_produce_68_percent_for_os_course(seeded_session) -> None:
    record = seeded_session.execute(
        select(AttendanceRecord)
        .join(Enrollment, AttendanceRecord.enrollment_id == Enrollment.id)
        .join(Student, Enrollment.student_id == Student.id)
        .join(Course, Enrollment.course_id == Course.id)
        .where(Student.student_code == DEMO_STUDENT_CODE, Course.code == "CS301")
    ).scalar_one()

    assert record.classes_attended == 34
    assert record.classes_conducted == 50
    percentage = 100 * record.classes_attended / record.classes_conducted
    assert round(percentage, 1) == 68.0


# ---------------------------------------------------------------------------
# Career scenarios (13.B)
# ---------------------------------------------------------------------------


def test_eligible_opportunity_scenario(seeded_session) -> None:
    opp = seeded_session.execute(
        select(Opportunity).where(Opportunity.title == "AI Software Engineering Intern")
    ).scalar_one()
    student = seeded_session.execute(
        select(Student).where(Student.student_code == DEMO_STUDENT_CODE)
    ).scalar_one()
    assert opp.status == OpportunityStatus.OPEN
    assert opp.minimum_cgpa <= student.cgpa
    assert opp.deadline > utc_now()
    assert student.department.code in {d.department.code for d in opp.allowed_departments}
    assert student.year in {y.year for y in opp.eligible_years}


def test_cgpa_blocked_opportunity_scenario(seeded_session) -> None:
    opp = seeded_session.execute(
        select(Opportunity).where(Opportunity.title == "Quant Research Intern")
    ).scalar_one()
    student = seeded_session.execute(
        select(Student).where(Student.student_code == DEMO_STUDENT_CODE)
    ).scalar_one()
    assert opp.minimum_cgpa > student.cgpa


def test_department_blocked_opportunity_scenario(seeded_session) -> None:
    opp = seeded_session.execute(
        select(Opportunity).where(Opportunity.title == "Embedded Systems Intern")
    ).scalar_one()
    allowed_codes = {d.department.code for d in opp.allowed_departments}
    assert "CSE" not in allowed_codes


def test_skill_gap_opportunity_scenario(seeded_session) -> None:
    opp = seeded_session.execute(
        select(Opportunity).where(Opportunity.title == "Cloud DevOps Intern")
    ).scalar_one()
    required_skill_names = {rs.skill.name for rs in opp.required_skills}

    from app.db.repositories.career import get_student_skills

    demo_skill_names = {ss.skill.name for ss in get_student_skills(seeded_session, DEMO_STUDENT_CODE)}
    assert required_skill_names.isdisjoint(demo_skill_names)


def test_expired_opportunity_scenario(seeded_session) -> None:
    opp = seeded_session.execute(
        select(Opportunity).where(Opportunity.title == "Data Analyst Intern")
    ).scalar_one()
    assert opp.status == OpportunityStatus.EXPIRED
    assert opp.deadline < utc_now()


# ---------------------------------------------------------------------------
# Event conflict scenarios (13.C)
# ---------------------------------------------------------------------------


def test_no_conflict_ai_workshop_scenario(seeded_session) -> None:
    event = seeded_session.execute(
        select(Event).where(Event.title == "Artificial Intelligence & Deep Learning Workshop")
    ).scalar_one()
    assert event.status == EventStatus.OPEN
    assert event.start_at.weekday() == 5  # Saturday: no classes scheduled that day


def test_timetable_conflicting_event_scenario(seeded_session) -> None:
    from datetime import timedelta, timezone

    event = seeded_session.execute(
        select(Event).where(Event.title == "Hackathon Kickoff Session")
    ).scalar_one()
    from app.db.repositories.academics import get_timetable

    slots = get_timetable(seeded_session, DEMO_STUDENT_CODE)
    os_slot = next(s for s in slots if s.course.code == "CS301")

    ist = timezone(timedelta(hours=5, minutes=30))
    event_start_ist = event.start_at.astimezone(ist)
    event_end_ist = event.end_at.astimezone(ist)

    # Python's Monday=0..Sunday=6 matches TimetableSlot.weekday's convention.
    assert event_start_ist.weekday() == os_slot.weekday
    # Overlap: event starts before the slot ends, and ends after the slot starts.
    assert event_start_ist.time() < os_slot.end_time
    assert event_end_ist.time() > os_slot.start_time


def test_exam_conflicting_event_scenario(seeded_session) -> None:
    event = seeded_session.execute(select(Event).where(Event.title == "Robotics Expo")).scalar_one()
    exam = seeded_session.execute(
        select(Exam)
        .join(Course, Exam.course_id == Course.id)
        .where(Course.code == "CS302")
    ).scalar_one()
    assert event.start_at < exam.scheduled_end
    assert event.end_at > exam.scheduled_start


def test_at_capacity_event_scenario(seeded_session) -> None:
    event = seeded_session.execute(select(Event).where(Event.title == "Startup Pitch Night")).scalar_one()
    registration_count = seeded_session.execute(
        select(func.count()).select_from(EventRegistration).where(EventRegistration.event_id == event.id)
    ).scalar_one()
    assert event.capacity == registration_count


def test_expired_closed_event_scenario(seeded_session) -> None:
    event = seeded_session.execute(
        select(Event).where(Event.title == "Freshers' Orientation Meet")
    ).scalar_one()
    assert event.status == EventStatus.COMPLETED
    assert event.end_at < utc_now()


# ---------------------------------------------------------------------------
# Campus cases (13.D) and duplicate-safe registration (13.E)
# ---------------------------------------------------------------------------


def test_normal_open_case_exists(seeded_session) -> None:
    case = seeded_session.execute(select(CampusCase).where(CampusCase.case_code == "CASE-0001")).scalar_one()
    assert case.status == CaseStatus.OPEN


def test_high_priority_case_exists(seeded_session) -> None:
    from app.db.models.services import CasePriority

    case = seeded_session.execute(select(CampusCase).where(CampusCase.case_code == "CASE-0002")).scalar_one()
    assert case.priority == CasePriority.HIGH


def test_sla_breached_case_exists(seeded_session) -> None:
    case = seeded_session.execute(select(CampusCase).where(CampusCase.case_code == "CASE-0003")).scalar_one()
    sla = seeded_session.execute(select(CaseSLA).where(CaseSLA.case_id == case.id)).scalar_one()
    assert sla.resolved_at is None
    assert sla.resolution_due_at < utc_now()


def test_existing_event_registration_for_duplicate_prevention_tests(seeded_session) -> None:
    event = seeded_session.execute(
        select(Event).where(Event.title == "Artificial Intelligence & Deep Learning Workshop")
    ).scalar_one()
    student = seeded_session.execute(
        select(Student).where(Student.student_code == DEMO_STUDENT_CODE)
    ).scalar_one()
    registration = seeded_session.execute(
        select(EventRegistration).where(
            EventRegistration.event_id == event.id, EventRegistration.student_id == student.id
        )
    ).scalar_one_or_none()
    assert registration is not None
