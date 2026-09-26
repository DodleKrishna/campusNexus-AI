"""Agent-as-a-Product MVP: catalog -> configure -> deploy -> run -> monitor, per organization."""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import AgentDeployment, AIUsageEvent
from app.llm.router import ADVANCED_MODEL, LIGHT_MODEL
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_ADMIN, A_FACULTY, A_STUDENT, B_ADMIN, B_STUDENT, app, bearer, client, login, orgs,
)

ASK = {"message": "What is my attendance in Operating Systems?"}


def headers(client, email):
    return bearer(login(client, email)["access_token"])


def deployment(client, admin, key):
    return next(d for d in client.get("/admin/agents/deployments", headers=admin).json() if d["agent_key"] == key)


def test_the_demo_organization_starts_with_six_default_deployments(client) -> None:
    catalog = client.get("/admin/agents/catalog", headers=headers(client, A_ADMIN)).json()
    assert {c["key"] for c in catalog} == {"academic", "attendance", "career", "events", "campus_services", "enquiry"}
    levels = {c["key"]: c["deployment"]["intelligence_level"] for c in catalog}
    assert levels == {"academic": "advanced", "attendance": "no_ai", "career": "advanced", "events": "light",
                      "campus_services": "light", "enquiry": "light"}
    assert all(c["deployment"]["status"] == "active" for c in catalog)


def test_admin_deploys_an_agent_into_its_own_organization(client, orgs, session_factory) -> None:
    b_admin = headers(client, B_ADMIN)
    assert all(c["deployment"] is None for c in client.get("/admin/agents/catalog", headers=b_admin).json())
    body = {"intelligence_level": "advanced", "monthly_budget_usd": 25, "requires_approval": True, "organization_id": orgs["a"]}
    deployed = client.post("/admin/agents/academic/deploy", json=body, headers=b_admin)
    assert deployed.status_code == 200, deployed.text
    view = deployed.json()
    assert (view["status"], view["intelligence_level"], view["monthly_budget_usd"], view["requires_approval"]) == ("active", "advanced", 25, True)
    with session_factory() as s:  # the client's organization_id was ignored: the token decides
        assert s.get(AgentDeployment, view["id"]).organization_id == orgs["b"]
    assert [d["agent_key"] for d in client.get("/admin/agents/deployments", headers=b_admin).json()] == ["academic"]


def test_organizations_never_see_or_change_each_others_deployments(client) -> None:
    a_admin, b_admin = headers(client, A_ADMIN), headers(client, B_ADMIN)
    a_academic = deployment(client, a_admin, "academic")
    assert client.get("/admin/agents/deployments", headers=b_admin).json() == []
    refused = client.patch(f"/admin/agents/deployments/{a_academic['id']}", json={"status": "paused"}, headers=b_admin)
    assert refused.status_code == 404
    assert deployment(client, a_admin, "academic")["status"] == "active"


def test_internal_components_can_never_be_deployed(client) -> None:
    admin = headers(client, A_ADMIN)
    for key in ("action", "action_agent", "verifier", "approval_gate"):
        response = client.post(f"/admin/agents/{key}/deploy", json={}, headers=admin)
        assert response.status_code == 400 and response.json()["detail"]["code"] == "AGENT_NOT_DEPLOYABLE", key
    assert client.post("/admin/agents/teleporter/deploy", json={}, headers=admin).status_code == 404


def test_students_cannot_deploy_or_configure(client) -> None:
    student = headers(client, A_STUDENT)
    assert client.get("/admin/agents/catalog", headers=student).status_code == 403
    assert client.post("/admin/agents/academic/deploy", json={}, headers=student).status_code == 403
    assert client.patch("/admin/agents/deployments/1", json={"status": "paused"}, headers=student).status_code == 403


def test_a_paused_agent_refuses_and_a_resumed_one_runs(client) -> None:
    admin, student = headers(client, A_ADMIN), headers(client, A_STUDENT)
    academic = deployment(client, admin, "academic")
    assert client.post("/agents/academic/query", json=ASK, headers=student).status_code == 200
    client.patch(f"/admin/agents/deployments/{academic['id']}", json={"status": "paused"}, headers=admin)
    refused = client.post("/agents/academic/query", json=ASK, headers=student)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "AGENT_DISABLED"
    assert "answer" not in refused.json()  # nothing fabricated
    client.patch(f"/admin/agents/deployments/{academic['id']}", json={"status": "active"}, headers=admin)
    assert client.post("/agents/academic/query", json=ASK, headers=student).status_code == 200


def test_an_undeployed_agent_or_disallowed_role_is_refused(client, orgs) -> None:
    refused = client.post("/agents/academic/query", json=ASK, headers=headers(client, B_STUDENT))  # B deployed nothing
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "AGENT_NOT_DEPLOYED"
    admin = headers(client, A_ADMIN)
    academic = deployment(client, admin, "academic")
    client.patch(f"/admin/agents/deployments/{academic['id']}", json={"allowed_roles": ["faculty"]}, headers=admin)
    refused = client.post("/agents/academic/query", json=ASK, headers=headers(client, A_STUDENT))
    assert refused.json()["detail"]["code"] == "AGENT_ROLE_NOT_ALLOWED"
    assert client.post("/faculty/agents/academic/query", json={"message": "Which classes do I have today?"},
                       headers=headers(client, A_FACULTY)).status_code == 200


def test_the_deployment_level_selects_the_model(client, session_factory) -> None:
    admin, student = headers(client, A_ADMIN), headers(client, A_STUDENT)
    academic = deployment(client, admin, "academic")
    for level, model in (("light", LIGHT_MODEL), ("advanced", ADVANCED_MODEL)):
        client.patch(f"/admin/agents/deployments/{academic['id']}", json={"intelligence_level": level}, headers=admin)
        assert client.post("/agents/academic/query", json=ASK, headers=student).status_code == 200
        with session_factory() as s:
            latest = s.execute(select(AIUsageEvent).where(AIUsageEvent.agent_key == "academic")
                               .order_by(AIUsageEvent.id.desc())).scalars().first()
        assert latest.model == model and latest.level.value == level and latest.run_id


def test_runs_show_up_in_deployed_agents_and_the_control_tower(client) -> None:
    admin, student = headers(client, A_ADMIN), headers(client, A_STUDENT)
    client.post("/agents/academic/query", json=ASK, headers=student)
    academic = deployment(client, admin, "academic")
    assert academic["runs"] >= 1 and academic["success_rate_percent"] == 100.0
    runs = client.get("/admin/ai-operations", headers=admin).json()["agent_runs"]
    row = next(r for r in runs if r["agent_key"] == "academic")
    assert (row["agent_name"], row["intelligence"], row["status"]) == ("Academic Agent", "advanced", "succeeded")
    assert row["models"] == [ADVANCED_MODEL] and row["organization"]
