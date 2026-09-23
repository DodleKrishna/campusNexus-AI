"""Tests for GET /admin/cases -- role restriction + contract shape."""
from __future__ import annotations


def test_student_cannot_view_admin_cases(api_client) -> None:
    response = api_client.get("/admin/cases", headers={"X-Demo-Identity": "student-demo"})
    assert response.status_code == 403


def test_admin_can_view_all_cases(api_client) -> None:
    response = api_client.get("/admin/cases", headers={"X-Demo-Identity": "admin-demo"})
    assert response.status_code == 200
    cases = response.json()
    assert len(cases) > 1  # real seeded cases across multiple students
    student_codes = {c["student_code"] for c in cases}
    assert len(student_codes) > 1  # genuinely cross-student, not one student's view

    for case in cases:
        assert case["case_code"]
        assert case["status"] in {"open", "in_progress", "resolved", "closed"}


def test_faculty_can_view_all_cases(api_client) -> None:
    response = api_client.get("/admin/cases", headers={"X-Demo-Identity": "faculty-demo"})
    assert response.status_code == 200
