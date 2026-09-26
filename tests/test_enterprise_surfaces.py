"""Enterprise admin surfaces: Command Center, Workflows, Proactive Monitors, Knowledge, Connectors."""
from __future__ import annotations

from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_ADMIN, A_STUDENT, B_ADMIN, app, bearer, client, login, orgs,
)


def headers(client, email):
    return bearer(login(client, email)["access_token"])


def test_command_center_reports_real_organization_figures(client) -> None:
    body = client.get("/admin/command-center", headers=headers(client, A_ADMIN)).json()
    kpis = {k["key"]: k for k in body["kpis"]}
    assert list(kpis) == ["agents", "workflows", "approvals", "sla", "no_ai", "spend"]
    assert kpis["agents"]["value"] == "6" and kpis["agents"]["note"] == "of 6 deployed"
    assert {h["key"] for h in body["health"]} == {"attendance", "requests", "approvals", "complaints", "deadlines"}
    assert next(h for h in body["health"] if h["key"] == "attendance")["value"] > 0  # seeded at-risk students
    assert len(body["workforce"]) == 6 and body["activity"]  # A's sign-ins are audited
    other = client.get("/admin/command-center", headers=headers(client, B_ADMIN)).json()
    assert {k["key"]: k["value"] for k in other["kpis"]}["agents"] == "0" and other["workforce"] == []


def test_workflows_expose_chain_sla_and_counts(client) -> None:
    student = headers(client, A_STUDENT)
    draft = client.post("/requests/prepare", json={"message": "I need permission to attend the coding contest."},
                        headers=student).json()["request"]
    client.post("/requests", json={"request_id": draft["request_id"]}, headers=student)
    cards = {w["key"]: w for w in client.get("/admin/workflows", headers=headers(client, A_ADMIN)).json()}
    assert list(cards) == ["student_permission", "faculty_leave", "attendance", "grievance", "placement"]
    assert cards["student_permission"]["pending"] == 1 and "Class faculty / mentor" in cards["student_permission"]["chain"]
    assert cards["attendance"]["status"] == "partial" and "roadmap" in cards["attendance"]["note"]  # honest labelling
    assert cards["grievance"]["pending"] > 0


def test_a_monitor_run_is_deterministic_and_audited(client) -> None:
    admin = headers(client, A_ADMIN)
    monitors = {m["key"]: m for m in client.get("/admin/monitors", headers=admin).json()}
    assert set(monitors) == {"attendance_risk", "approval_sla", "placement_deadline", "exam_risk", "grievance_sla"}
    assert monitors["attendance_risk"]["findings_count"] > 0 and monitors["attendance_risk"]["last_checked"] is None
    run = client.post("/admin/monitors/attendance_risk/run", headers=admin).json()
    assert run["findings_count"] == monitors["attendance_risk"]["findings_count"] and run["last_checked"]
    assert any(e["action"] == "monitor_run" for e in client.get("/admin/audit", headers=admin).json())
    assert client.post("/admin/monitors/crystal_ball/run", headers=admin).status_code == 404


def test_knowledge_and_connectors_are_honest(client) -> None:
    admin = headers(client, A_ADMIN)
    hub = client.get("/admin/knowledge", headers=admin).json()
    attendance = next(d for d in hub["documents"] if d["document_type"] == "attendance_policy")
    assert attendance["agents"]["academic"] and attendance["agents"]["attendance"] and not attendance["agents"]["career"]
    assert "next hardening step" in hub["scope_note"]
    connectors = {c["key"]: c["status"] for c in client.get("/admin/connectors", headers=admin).json()}
    assert connectors["campusnexus_db"] == "connected"
    assert [k for k, v in connectors.items() if v == "connected"] == ["campusnexus_db"]  # nothing else is claimed live
    assert connectors["csv"] == "coming_next"


def test_students_cannot_open_enterprise_surfaces(client) -> None:
    student = headers(client, A_STUDENT)
    for path in ("/admin/command-center", "/admin/workflows", "/admin/monitors", "/admin/knowledge", "/admin/connectors"):
        assert client.get(path, headers=student).status_code == 403, path
