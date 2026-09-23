"""Tests for /demo/students, /students/{id}/dashboard, /students/{id}/calendar.

Covers spec §14 priorities: contract shape, dashboard correctness against
real seeded data, and student/admin ownership separation.
"""
from __future__ import annotations

DEMO_STUDENT = "STU-DEMO-001"
OTHER_STUDENT = "STU2023002"


def test_list_demo_students_requires_no_identity(api_client) -> None:
    response = api_client.get("/demo/students")
    assert response.status_code == 200
    keys = {s["key"] for s in response.json()}
    assert keys == {"student-demo", "student-alt"}
    for entry in response.json():
        assert entry["student_id"] is not None
        assert "(Student)" in entry["display_name"]


def test_dashboard_missing_identity_header_is_401(api_client) -> None:
    response = api_client.get(f"/students/{DEMO_STUDENT}/dashboard")
    assert response.status_code == 401


def test_dashboard_unknown_identity_is_401(api_client) -> None:
    response = api_client.get(f"/students/{DEMO_STUDENT}/dashboard", headers={"X-Demo-Identity": "not-a-real-key"})
    assert response.status_code == 401


def test_dashboard_returns_real_seeded_data(api_client) -> None:
    response = api_client.get(f"/students/{DEMO_STUDENT}/dashboard", headers={"X-Demo-Identity": "student-demo"})
    assert response.status_code == 200
    body = response.json()

    assert body["student"]["student_code"] == DEMO_STUDENT
    assert body["student"]["full_name"]
    assert isinstance(body["attendance"], list) and len(body["attendance"]) > 0
    # Attendance is grounded in the real policy threshold, not invented.
    for item in body["attendance"]:
        assert item["threshold_status"] == "ok"
        assert item["required_percentage"] is not None
    assert isinstance(body["grievances"], list) and len(body["grievances"]) > 0
    assert isinstance(body["calendar"], list)


def test_student_cannot_view_another_students_dashboard(api_client) -> None:
    response = api_client.get(f"/students/{OTHER_STUDENT}/dashboard", headers={"X-Demo-Identity": "student-demo"})
    assert response.status_code == 403


def test_admin_can_view_any_students_dashboard(api_client) -> None:
    response = api_client.get(f"/students/{OTHER_STUDENT}/dashboard", headers={"X-Demo-Identity": "admin-demo"})
    assert response.status_code == 200
    assert response.json()["student"]["student_code"] == OTHER_STUDENT


def test_dashboard_unknown_student_is_404_for_admin(api_client) -> None:
    response = api_client.get("/students/STU-NOPE-999/dashboard", headers={"X-Demo-Identity": "admin-demo"})
    assert response.status_code == 404


def test_calendar_ownership_separation(api_client) -> None:
    own = api_client.get(f"/students/{DEMO_STUDENT}/calendar", headers={"X-Demo-Identity": "student-demo"})
    assert own.status_code == 200
    assert isinstance(own.json(), list)

    other = api_client.get(f"/students/{OTHER_STUDENT}/calendar", headers={"X-Demo-Identity": "student-demo"})
    assert other.status_code == 403

    as_admin = api_client.get(f"/students/{OTHER_STUDENT}/calendar", headers={"X-Demo-Identity": "admin-demo"})
    assert as_admin.status_code == 200
