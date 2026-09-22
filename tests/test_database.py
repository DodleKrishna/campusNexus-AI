"""Tests for the SQLAlchemy persistence foundation: engine/session setup,
database isolation, datetime handling, FK enforcement, and repository reads.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from app.db.base import Base, UTCDateTime, utc_now
from app.db.models.academic import AttendanceRecord, Enrollment
from app.db.models.identity import Department, Student, User
from app.db.models.mission import Mission, MissionStep
from app.db.repositories import academics as academics_repo
from app.db.repositories import career as career_repo
from app.db.repositories import cases as cases_repo
from app.db.repositories import events as events_repo
from app.db.repositories import students as students_repo
from app.db.session import BASE_DIR, create_db_engine, get_database_url
from app.schemas.enums import AgentName, MissionStatus, TaskStatus, UserRole
from scripts.seed_data import DEMO_STUDENT_CODE, run_seed


# ---------------------------------------------------------------------------
# Database initialization
# ---------------------------------------------------------------------------


def test_database_initializes_all_tables(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    expected = {
        "departments", "users", "students",
        "courses", "enrollments", "attendance_records", "timetable_slots", "exams",
        "companies", "skills", "student_skills", "opportunities",
        "opportunity_departments", "opportunity_eligible_years", "opportunity_skills", "applications",
        "clubs", "events", "event_registrations",
        "campus_cases", "case_assignments", "case_slas",
        "notifications", "calendar_events",
        "missions", "mission_steps", "agent_runs", "tool_call_records",
        "approval_records", "audit_logs", "memory_records",
    }
    assert expected.issubset(table_names)


def test_all_mapped_tables_are_registered_on_base_metadata() -> None:
    assert set(Base.metadata.tables.keys())


# ---------------------------------------------------------------------------
# Dev / test database isolation
# ---------------------------------------------------------------------------


def test_default_database_url_points_at_repo_data_dir(monkeypatch) -> None:
    monkeypatch.delenv("CAMPUSNEXUS_DB_PATH", raising=False)
    url = get_database_url()
    assert url == f"sqlite:///{(BASE_DIR / 'data' / 'campusnexus.db').as_posix()}"


def test_env_var_overrides_default_database_path(monkeypatch, tmp_path) -> None:
    custom = tmp_path / "custom.db"
    monkeypatch.setenv("CAMPUSNEXUS_DB_PATH", str(custom))
    url = get_database_url()
    assert url == f"sqlite:///{custom.as_posix()}"


def test_explicit_db_path_overrides_env_var(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_DB_PATH", str(tmp_path / "env.db"))
    explicit = tmp_path / "explicit.db"
    url = get_database_url(db_path=explicit)
    assert url == f"sqlite:///{explicit.as_posix()}"


def test_test_engine_never_touches_development_database(tmp_path, monkeypatch) -> None:
    dev_db_path = BASE_DIR / "data" / "campusnexus.db"
    existed_before = dev_db_path.exists()
    mtime_before = dev_db_path.stat().st_mtime if existed_before else None

    monkeypatch.setenv("CAMPUSNEXUS_DB_PATH", str(tmp_path / "isolated.db"))
    engine = create_db_engine()
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    engine.dispose()

    assert (tmp_path / "isolated.db").exists()
    if existed_before:
        assert dev_db_path.stat().st_mtime == mtime_before
    else:
        assert not dev_db_path.exists()


# ---------------------------------------------------------------------------
# UTCDateTime
# ---------------------------------------------------------------------------


def test_utc_datetime_round_trips_as_timezone_aware(session) -> None:
    dept = Department(code="TST", name="Test Department")
    session.add(dept)
    session.commit()

    user = User(email="tz-test@meridian.edu", full_name="TZ Tester", role=UserRole.STUDENT)
    session.add(user)
    session.commit()

    student = Student(
        student_code="STU-TZ-001", user_id=user.id, department_id=dept.id,
        year=1, semester=1, cgpa=8.0,
    )
    session.add(student)
    session.commit()
    session.refresh(student)

    assert student.created_at.tzinfo is not None
    assert student.created_at.utcoffset() == timedelta(0)


def test_utc_datetime_rejects_naive_input(session) -> None:
    dept = Department(code="TST2", name="Test Department 2")
    session.add(dept)
    session.commit()

    naive_created_at = datetime(2025, 1, 1)  # no tzinfo
    dept2 = Department(code="TST3", name="Naive", created_at=naive_created_at)
    session.add(dept2)
    with pytest.raises(StatementError, match="UTCDateTime requires a timezone-aware datetime"):
        session.commit()
    session.rollback()


def test_ist_input_is_normalized_to_utc_on_read(session) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    dept = Department(code="TST4", name="IST Dept", created_at=datetime(2025, 6, 1, 15, 30, tzinfo=ist))
    session.add(dept)
    session.commit()
    session.refresh(dept)

    assert dept.created_at == datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Foreign key enforcement
# ---------------------------------------------------------------------------


def test_invalid_foreign_key_is_rejected(session) -> None:
    orphan_step = MissionStep(
        step_id="step-orphan",
        mission_id="mission-does-not-exist",
        agent=AgentName.ACADEMIC_AGENT,
        objective="Should not be insertable",
        status=TaskStatus.PENDING,
    )
    session.add(orphan_step)
    with pytest.raises(IntegrityError):
        session.commit()


def test_valid_foreign_key_is_accepted(session) -> None:
    mission = Mission(
        mission_id="mission-1", user_id=DEMO_STUDENT_CODE, user_role=UserRole.STUDENT,
        original_goal="Plan my semester", status=MissionStatus.PENDING,
    )
    session.add(mission)
    session.commit()

    step = MissionStep(
        step_id="step-1", mission_id="mission-1", agent=AgentName.ACADEMIC_AGENT,
        objective="Check attendance", status=TaskStatus.PENDING,
    )
    session.add(step)
    session.commit()
    session.refresh(step)
    assert step.mission_id == "mission-1"


# ---------------------------------------------------------------------------
# Repository reads (against seeded data)
# ---------------------------------------------------------------------------


def test_get_student_by_id_repository(seeded_session) -> None:
    student = students_repo.get_student_by_id(seeded_session, DEMO_STUDENT_CODE)
    assert student is not None
    assert student.student_code == DEMO_STUDENT_CODE
    assert student.department.code == "CSE"


def test_get_student_by_id_returns_none_for_unknown_student(seeded_session) -> None:
    assert students_repo.get_student_by_id(seeded_session, "STU-NOT-REAL") is None


def test_get_attendance_records_repository_returns_raw_counts(seeded_session) -> None:
    records = academics_repo.get_attendance_records(seeded_session, DEMO_STUDENT_CODE)
    assert len(records) >= 4
    os_record = next(r for r in records if r.enrollment.course.code == "CS301")
    assert os_record.classes_attended == 34
    assert os_record.classes_conducted == 50


def test_get_timetable_repository(seeded_session) -> None:
    slots = academics_repo.get_timetable(seeded_session, DEMO_STUDENT_CODE)
    course_codes = {slot.course.code for slot in slots}
    assert "CS301" in course_codes


def test_get_exam_schedule_repository(seeded_session) -> None:
    exams = academics_repo.get_exam_schedule(seeded_session, DEMO_STUDENT_CODE)
    assert any(exam.course.code == "CS302" for exam in exams)


def test_list_active_opportunities_repository(seeded_session) -> None:
    opportunities = career_repo.list_active_opportunities(seeded_session)
    titles = {o.title for o in opportunities}
    assert "AI Software Engineering Intern" in titles
    assert "Data Analyst Intern" not in titles  # expired, not OPEN


def test_get_student_skills_repository(seeded_session) -> None:
    skills = career_repo.get_student_skills(seeded_session, DEMO_STUDENT_CODE)
    skill_names = {s.skill.name for s in skills}
    assert "Python" in skill_names
    assert "Docker" not in skill_names


def test_list_upcoming_events_repository(seeded_session) -> None:
    events = events_repo.list_upcoming_events(seeded_session, utc_now())
    assert any(e.title == "Artificial Intelligence & Deep Learning Workshop" for e in events)


def test_get_student_registrations_repository(seeded_session) -> None:
    registrations = events_repo.get_student_registrations(seeded_session, DEMO_STUDENT_CODE)
    assert any(
        r.event.title == "Artificial Intelligence & Deep Learning Workshop" for r in registrations
    )


def test_get_student_cases_repository(seeded_session) -> None:
    cases = cases_repo.get_student_cases(seeded_session, DEMO_STUDENT_CODE)
    case_codes = {c.case_code for c in cases}
    assert {"CASE-0001", "CASE-0002", "CASE-0003"}.issubset(case_codes)


def test_get_case_sla_repository(seeded_session) -> None:
    sla = cases_repo.get_case_sla(seeded_session, "CASE-0003")
    assert sla is not None
    assert sla.resolved_at is None
