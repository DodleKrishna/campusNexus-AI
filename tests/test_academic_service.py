"""Tests for the read-only Academic Service (app/services/academic.py)."""
from __future__ import annotations

from app.services import academic as academic_service

DEMO_STUDENT = "STU-DEMO-001"


def test_get_student_academic_profile_returns_enrollments(seeded_session) -> None:
    profile = academic_service.get_student_academic_profile(seeded_session, DEMO_STUDENT)
    assert profile is not None
    assert profile.student_code == DEMO_STUDENT
    assert profile.full_name == "Aditi Rao"
    assert profile.department_code == "CSE"
    course_codes = {c.course_code for c in profile.courses}
    assert course_codes == {"CS301", "CS302", "CS303", "CS304"}


def test_get_student_academic_profile_unknown_student_returns_none(seeded_session) -> None:
    assert academic_service.get_student_academic_profile(seeded_session, "STU-NOPE-999") is None


def test_get_attendance_returns_raw_counts_for_all_courses(seeded_session) -> None:
    snapshots = academic_service.get_attendance(seeded_session, DEMO_STUDENT)
    by_course = {s.course_code: s for s in snapshots}
    assert by_course["CS301"].classes_attended == 34
    assert by_course["CS301"].classes_conducted == 50
    assert by_course["CS302"].classes_attended == 47
    assert by_course["CS302"].classes_conducted == 50


def test_get_attendance_filters_by_course_code_case_insensitive(seeded_session) -> None:
    snapshots = academic_service.get_attendance(seeded_session, DEMO_STUDENT, course_code="cs301")
    assert len(snapshots) == 1
    assert snapshots[0].course_code == "CS301"
    assert snapshots[0].classes_attended == 34
    assert snapshots[0].classes_conducted == 50


def test_get_timetable_returns_entries_for_enrolled_courses(seeded_session) -> None:
    entries = academic_service.get_timetable(seeded_session, DEMO_STUDENT)
    course_codes = {e.course_code for e in entries}
    assert course_codes == {"CS301", "CS302", "CS303", "CS304"}
    os_entry = next(e for e in entries if e.course_code == "CS301")
    assert os_entry.weekday == 0
    assert os_entry.location == "Block A - Room 204"


def test_get_exam_schedule_returns_entries_for_enrolled_courses(seeded_session) -> None:
    entries = academic_service.get_exam_schedule(seeded_session, DEMO_STUDENT)
    course_codes = {e.course_code for e in entries}
    assert course_codes == {"CS301", "CS302", "CS303", "CS304"}
    assert all(e.exam_type == "midterm" for e in entries)


def test_get_course_returns_enrolled_course(seeded_session) -> None:
    course = academic_service.get_course(seeded_session, DEMO_STUDENT, "CS301")
    assert course is not None
    assert course.title == "Operating Systems"
    assert course.credits == 4


def test_get_course_returns_none_when_not_enrolled(seeded_session) -> None:
    # CS401 exists in the catalog but the demo student (semester 5) isn't enrolled in it.
    assert academic_service.get_course(seeded_session, DEMO_STUDENT, "CS401") is None


def test_get_course_returns_none_for_unknown_code(seeded_session) -> None:
    assert academic_service.get_course(seeded_session, DEMO_STUDENT, "ZZ999") is None
