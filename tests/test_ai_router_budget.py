"""Hackathon fast-finish: adaptive intelligence router, AI telemetry, organization AI budget, Agent Catalog."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AIUsageEvent, Mission
from app.llm.base import LLMProviderError
from app.llm.providers.mock import MockLLMProvider
from app.llm.router import (
    ADVANCED_MODEL, AI_BUDGET_EXCEEDED, LIGHT_MODEL, AIBudgetExceededError, RoutedLLMProvider, ai_context,
    estimate_cost, no_ai, route,
)
from app.schemas.enums import IntelligenceLevel
from tests.test_phase10_mission_flow import REGISTER_GOAL
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_ADMIN, A_FACULTY, A_STUDENT, B_ADMIN, app, bearer, client, login, orgs,
)


def headers(client, email):
    return bearer(login(client, email)["access_token"])


class Recording(MockLLMProvider):
    """A mock that reports which model it stands for, optionally fails, and reports token usage."""

    def __init__(self, model: str, *, fail: bool = False, usage=None, live: bool = False) -> None:
        super().__init__()
        self._model, self._fail, self._usage, self.is_live, self.calls = model, fail, usage, live, []

    @property
    def model_name(self):
        return self._model

    def take_last_usage(self):
        return self._usage

    def plan_enquiry(self, query):
        self.calls.append("plan_enquiry")
        return super().plan_enquiry(query)

    def plan_mission(self, *a, **k):
        self.calls.append("plan_mission")
        if self._fail:
            raise LLMProviderError("the strong model refused")
        return super().plan_mission(*a, **k)


# --- Routing decisions ----------------------------------------------------------------------------------------


def test_the_level_and_model_are_chosen_before_execution() -> None:
    planning = route("plan_mission")
    assert (planning.level, planning.model) == (IntelligenceLevel.ADVANCED, ADVANCED_MODEL) == (IntelligenceLevel.ADVANCED, "openai/gpt-oss-120b")
    for operation in ("classify_academic_intent", "plan_enquiry", "plan_permission_request", "generate_events_response"):
        decision = route(operation)
        assert (decision.level, decision.model) == (IntelligenceLevel.LIGHT, LIGHT_MODEL) == (IntelligenceLevel.LIGHT, "openai/gpt-oss-20b")
        assert decision.reason
    deterministic = no_ai("request_submit", "deterministic routing")
    assert (deterministic.level, deterministic.model) == (IntelligenceLevel.NO_AI, None)


def test_each_operation_goes_to_its_levels_provider() -> None:
    light, advanced = Recording(LIGHT_MODEL), Recording(ADVANCED_MODEL)
    router = RoutedLLMProvider(light=light, advanced=advanced)
    router.plan_enquiry("what is my attendance?")
    assert light.calls == ["plan_enquiry"] and advanced.calls == []


def test_a_failed_strong_call_is_never_downgraded() -> None:
    light, advanced = Recording(LIGHT_MODEL), Recording(ADVANCED_MODEL, fail=True)
    router = RoutedLLMProvider(light=light, advanced=advanced)
    with ai_context(None) as context:
        with pytest.raises(LLMProviderError, match="refused"):
            router.plan_mission("Plan my semester", available_agents=[])
    assert advanced.calls == ["plan_mission"] and light.calls == []  # no silent 20B retry
    assert [(r.level, r.success, r.error_kind) for r in context.records] == [(IntelligenceLevel.ADVANCED, False, "LLMProviderError")]


def test_tokens_and_cost_are_recorded_only_when_reported() -> None:
    reported = RoutedLLMProvider(light=Recording(LIGHT_MODEL, usage=(1000, 200), live=True), advanced=Recording(ADVANCED_MODEL))
    silent = RoutedLLMProvider(light=Recording(LIGHT_MODEL, usage=None, live=True), advanced=Recording(ADVANCED_MODEL))
    with ai_context(None) as context:
        reported.plan_enquiry("q")
        silent.plan_enquiry("q")
    first, second = context.records
    assert (first.input_tokens, first.output_tokens) == (1000, 200)
    assert first.estimated_cost_usd == estimate_cost(LIGHT_MODEL, 1000, 200) and first.estimated_cost_usd > 0
    assert (second.input_tokens, second.output_tokens, second.estimated_cost_usd) == (None, None, None)  # unavailable


# --- Telemetry through the API --------------------------------------------------------------------------------


def test_ai_calls_and_no_ai_resolutions_are_metered_per_organization(client, orgs, session_factory) -> None:
    student = headers(client, A_STUDENT)
    assert client.post("/agents/enquiry/query", json={"message": "What is my attendance?"}, headers=student).status_code == 200
    draft = client.post("/requests/prepare", json={"message": "I need permission to attend the coding contest."},
                        headers=student).json()["request"]
    assert client.post("/requests", json={"request_id": draft["request_id"]}, headers=student).status_code == 200
    ops = client.get("/admin/ai-operations", headers=headers(client, A_ADMIN)).json()
    usage = ops["usage"]
    assert usage["light_calls"] >= 2 and usage["no_ai_count"] >= 1 and 0 < usage["no_ai_percent"] < 100
    assert usage["success_rate_percent"] == 100.0 and usage["estimated_cost_usd"] is None  # mock: no tokens reported
    assert ops["routing"] == {"no_ai": "deterministic rules / workflow", "light": LIGHT_MODEL, "advanced": ADVANCED_MODEL}
    with session_factory() as s:
        assert {e.organization_id for e in s.execute(select(AIUsageEvent)).scalars()} == {orgs["a"]}
    assert client.get("/admin/ai-operations", headers=headers(client, B_ADMIN)).json()["usage"]["total_requests"] == 0


def test_mission_ai_calls_in_worker_threads_are_attributed_to_the_mission(client, orgs, session_factory) -> None:
    mission = client.post("/missions", json={"goal": REGISTER_GOAL}, headers={"X-Demo-Identity": "student-demo"}).json()
    with session_factory() as s:
        events = s.execute(select(AIUsageEvent).where(AIUsageEvent.mission_id == mission["mission_id"])).scalars().all()
    levels = {e.level for e in events}
    assert IntelligenceLevel.ADVANCED in levels and IntelligenceLevel.LIGHT in levels  # planner + agents in dispatcher threads
    assert {e.organization_id for e in events} == {orgs["a"]}
    tower = client.get("/admin/ai-operations", headers=headers(client, A_ADMIN)).json()["control_tower"]
    row = next(r for r in tower if r["mission_id"] == mission["mission_id"])
    assert row["intelligence"] == "advanced" and row["approvals_required"] >= 1 and row["organization"]


# --- Budget ---------------------------------------------------------------------------------------------------


def test_an_exhausted_budget_stops_ai_but_not_deterministic_work(client, orgs) -> None:
    admin, student, faculty = headers(client, A_ADMIN), headers(client, A_STUDENT), headers(client, A_FACULTY)
    assert client.put("/admin/ai-budget", json={"monthly_budget_usd": 0}, headers=admin).json()["monthly_budget_usd"] == 0
    refused = client.post("/agents/enquiry/query", json={"message": "What is my attendance?"}, headers=student)
    assert refused.status_code == 402 and refused.json()["detail"]["code"] == AI_BUDGET_EXCEEDED
    refused = client.post("/requests/prepare", json={"message": "I need leave tomorrow."}, headers=student)
    assert refused.status_code == 402 and refused.json()["detail"]["code"] == AI_BUDGET_EXCEEDED
    # Deterministic features keep working.
    for path, who in (("/me/dashboard", student), ("/me/attendance", student), ("/faculty/dashboard", faculty),
                      ("/admin/dashboard", admin)):
        assert client.get(path, headers=who).status_code == 200, path
    # The budget is per organization: B is unaffected.
    assert client.get("/admin/ai-operations", headers=headers(client, B_ADMIN)).json()["usage"]["monthly_budget_usd"] is None
    # Lifting the budget restores AI.
    client.put("/admin/ai-budget", json={"monthly_budget_usd": None}, headers=admin)
    assert client.post("/agents/enquiry/query", json={"message": "What is my attendance?"}, headers=student).status_code == 200


def test_a_mission_under_an_exhausted_budget_fails_visibly(client, orgs, session_factory) -> None:
    client.put("/admin/ai-budget", json={"monthly_budget_usd": 0}, headers=headers(client, A_ADMIN))
    response = client.post("/missions", json={"goal": "Check my Operating Systems attendance"}, headers={"X-Demo-Identity": "student-demo"})
    with session_factory() as s:
        mission = s.execute(select(Mission).order_by(Mission.created_at.desc())).scalars().first()
    assert mission.status.value == "failed" and AI_BUDGET_EXCEEDED in (mission.final_result or "") + response.text


def test_budget_error_is_explicit() -> None:
    error = AIBudgetExceededError(1, 5.0, 5.2)
    assert isinstance(error, LLMProviderError) and error.code == AI_BUDGET_EXCEEDED and AI_BUDGET_EXCEEDED in str(error)


# --- Agent Catalog --------------------------------------------------------------------------------------------


def test_the_agent_catalog_lists_product_agents_and_keeps_internals_internal(client) -> None:
    catalog = client.get("/admin/agent-catalog", headers=headers(client, A_ADMIN)).json()
    names = {a["name"] for a in catalog["agents"]}
    assert {"Academic Agent", "Attendance Agent", "Career / Placement Agent", "Events Agent", "Campus Services Agent",
            "Enquiry Agent"} == names
    assert all(a["deployable"] for a in catalog["agents"])
    assert {a["name"] for a in catalog["internal_components"]} == {"Action Agent", "Deterministic Verifier", "Approval Gate"}
    assert not any(a["deployable"] for a in catalog["internal_components"]) and not names & {"Action Agent", "Approval Gate"}
    assert client.get("/admin/agent-catalog", headers=headers(client, A_STUDENT)).status_code == 403
