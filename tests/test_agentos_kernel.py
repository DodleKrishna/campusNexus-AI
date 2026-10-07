"""AgentOS V2 Phase 1: the durable, bounded Agent Kernel (fake brain + fake read-only tools; no external calls)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agentos.brain import BrainUnavailableError, ScriptedAgentBrain
from app.agentos.events import SUBJECT_MISSION, EventService
from app.agentos.registry import (
    AgentRegistry, AgentSpec, AgentTool, RegistryError, ToolContext, ToolRegistry, ToolResult,
)
from app.agentos.runtime import HARD_TRANSITION_CAP, AgentOSError, AgentRuntime, MissionActor
from app.agentos.schemas import AgentDecision, CreateAgentMission, DecisionKind, DomainEventType
from app.db.models import (
    AgentMission, AgentMissionStatus, AgentStep, AgentStepStatus, AuthAccount, DomainEvent, OperationAuditEvent,
)
from app.db.tenant_session import TenantSessionFactory
from app.schemas.enums import UserRole
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_ADMIN, A_STUDENT, B_ADMIN, B_STUDENT, app, bearer, client, login, orgs,
)

STUDENT, ADMIN = UserRole.STUDENT, UserRole.ADMIN
PHONE = "+91 98765 43210"


class LookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course_code: str = Field(pattern=r"^[A-Z]{2,4}\d{3}$")


class FakeCourseLookup(AgentTool):
    name = "fake_course_lookup"
    description = "Look up a course (fake, read-only)."
    input_model = LookupInput
    required_roles = frozenset({STUDENT, ADMIN})

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, context: ToolContext, args: LookupInput) -> ToolResult:
        self.calls.append(args.course_code)
        return ToolResult(ok=True, data={"course_code": args.course_code, "credits": 4, "faculty_phone": PHONE})


class FakeGrades(FakeCourseLookup):
    name = "fake_grades"


@pytest.fixture()
def tools():
    return {"lookup": FakeCourseLookup(), "grades": FakeGrades()}


def build_runtime(tools, script, clock=None) -> AgentRuntime:
    agents = AgentRegistry()
    agents.register(AgentSpec("study_planner", "plans study", allowed_tools=frozenset({"fake_course_lookup"}),
                              allowed_delegate_agents=frozenset({"reminder_agent"}), supported_roles=frozenset({STUDENT})))
    agents.register(AgentSpec("reminder_agent", "reminds", supported_roles=frozenset({STUDENT})))
    agents.register(AgentSpec("secret_agent", "not delegable", supported_roles=frozenset({STUDENT})))
    registry = ToolRegistry()
    registry.register(tools["lookup"])
    registry.register(tools["grades"])
    kwargs = {"clock": clock} if clock else {}
    return AgentRuntime(agents, registry, ScriptedAgentBrain(script), **kwargs)


@pytest.fixture()
def kernel(orgs, engine, session_factory):
    with session_factory() as s:
        ids = {e: s.execute(select(AuthAccount.id).where(AuthAccount.email == e)).scalar_one()
               for e in (A_STUDENT, A_ADMIN, B_STUDENT, B_ADMIN)}
    factory = TenantSessionFactory(engine)
    actors = {
        "a_student": MissionActor(orgs["a"], ids[A_STUDENT], STUDENT), "a_admin": MissionActor(orgs["a"], ids[A_ADMIN], ADMIN),
        "b_student": MissionActor(orgs["b"], ids[B_STUDENT], STUDENT), "b_admin": MissionActor(orgs["b"], ids[B_ADMIN], ADMIN),
    }
    return factory, actors


def new_mission(runtime, factory, actor, **overrides) -> int:
    body = CreateAgentMission(**{"agent_key": "study_planner", "goal": "Plan my OS revision", **overrides})
    with factory.open_tenant_session(actor.organization_id) as s:
        return runtime.create_mission(s, actor, body).id


def run(runtime, factory, actor, mission_id):
    with factory.open_tenant_session(actor.organization_id) as s:
        return runtime.run_step(s, actor, mission_id)


def load(factory, org, mission_id):
    with factory.open_tenant_session(org) as s:
        mission = s.get(AgentMission, mission_id)
        steps = list(s.execute(select(AgentStep).where(AgentStep.mission_id == mission_id).order_by(AgentStep.step_number)).scalars())
        audits = [e.event_type for e in s.execute(select(OperationAuditEvent).where(
            OperationAuditEvent.subject_type == "agent_mission", OperationAuditEvent.subject_id == str(mission_id))
            .order_by(OperationAuditEvent.id)).scalars()]
        return mission, steps, audits


TOOL = AgentDecision(kind=DecisionKind.TOOL, tool_name="fake_course_lookup", tool_input={"course_code": "CS301"})
COMPLETE = AgentDecision(kind=DecisionKind.COMPLETE, outcome="Revision plan ready.")


# --- Persistence and one step -----------------------------------------------------------------------------------


def test_mission_persists_in_the_callers_organization(kernel, tools) -> None:
    factory, actors = kernel
    runtime = build_runtime(tools, [])
    mid = new_mission(runtime, factory, actors["a_student"], success_criteria=["covers units 1-3"], max_steps=7,
                      context={"term": "odd", "api_key": "sk-live-123", "contact": PHONE})
    mission, steps, audits = load(factory, actors["a_student"].organization_id, mid)
    assert (mission.status, mission.step_count, mission.max_steps, steps) == (AgentMissionStatus.PENDING, 0, 7, [])
    assert mission.organization_id == actors["a_student"].organization_id
    assert mission.owner_account_id == mission.created_by_account_id == actors["a_student"].account_id
    assert mission.success_criteria == ["covers units 1-3"]
    assert mission.context["inputs"] == {"term": "odd", "api_key": "[redacted]", "contact": "[redacted]"}
    assert audits == ["MISSION_CREATED"]


def test_one_step_runs_one_allowed_tool(kernel, tools) -> None:
    factory, actors = kernel
    runtime = build_runtime(tools, [TOOL])
    mid = new_mission(runtime, factory, actors["a_student"])
    result = run(runtime, factory, actors["a_student"], mid)
    assert (result.transitioned, result.status, result.step_number, result.step_status) == (
        True, AgentMissionStatus.RUNNING, 1, AgentStepStatus.EXECUTED)
    assert tools["lookup"].calls == ["CS301"] and tools["grades"].calls == []
    mission, steps, audits = load(factory, actors["a_student"].organization_id, mid)
    assert mission.step_count == 1 and len(steps) == 1
    step = steps[0]
    assert (step.action_type, step.tool_name, step.status) == ("tool", "fake_course_lookup", AgentStepStatus.EXECUTED)
    assert step.output_summary["data"]["faculty_phone"] == "[redacted]"
    assert audits == ["MISSION_CREATED", "MISSION_STARTED", "TOOL_EXECUTED", "AGENT_STEP_EXECUTED"]
    seen = runtime.brain.seen[0]  # the brain saw only its allowlisted tools and no secrets
    assert [t.name for t in seen.allowed_tools] == ["fake_course_lookup"]
    assert seen.allowed_delegate_agents == ["reminder_agent"] and seen.observations[0].kind == "mission_started"


# --- Rejections -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("decision, code", [
    ({"kind": "tool", "tool_name": "fake_grades", "tool_input": {"course_code": "CS301"}}, "UNAUTHORIZED_TOOL"),
    ({"kind": "tool", "tool_name": "os_system", "tool_input": {}}, "UNAUTHORIZED_TOOL"),
    ({"kind": "tool", "tool_name": "fake_course_lookup", "tool_input": {"course_code": "1; DROP TABLE x"}}, "INVALID_TOOL_INPUT"),
    ({"kind": "tool", "tool_name": "fake_course_lookup", "tool_input": {"course_code": "CS301", "sql": "x"}}, "INVALID_TOOL_INPUT"),
    ({"kind": "tool", "tool_name": "app.tools:exec", "tool_input": {}}, "INVALID_DECISION"),
    ({"kind": "delegate", "delegate_agent": "secret_agent", "delegate_goal": "x"}, "UNAUTHORIZED_DELEGATION"),
    ({"kind": "delegate", "delegate_agent": "https://evil.example", "delegate_goal": "x"}, "INVALID_DECISION"),
    ({"kind": "shell", "command": "rm -rf /"}, "INVALID_DECISION"),
    ({"kind": {"nested": "x"}}, "INVALID_DECISION"),
    ({"kind": "complete", "outcome": "done", "tool_name": "fake_course_lookup"}, "INVALID_DECISION"),
])
def test_invalid_decisions_are_rejected_and_fail_the_mission(kernel, tools, decision, code) -> None:
    factory, actors = kernel
    runtime = build_runtime(tools, [decision])
    mid = new_mission(runtime, factory, actors["a_student"])
    result = run(runtime, factory, actors["a_student"], mid)
    assert (result.status, result.step_status, result.error_code) == (AgentMissionStatus.FAILED, AgentStepStatus.REJECTED, code)
    assert tools["lookup"].calls == [] and tools["grades"].calls == []
    mission, steps, audits = load(factory, actors["a_student"].organization_id, mid)
    assert mission.context["failure"]["code"] == code and mission.completed_at is not None
    assert [s.status for s in steps] == [AgentStepStatus.REJECTED]
    assert audits[-2:] == ["AGENT_STEP_EXECUTED", "MISSION_FAILED"]
    with factory.open_tenant_session(mission.organization_id) as s:
        assert s.execute(select(AgentMission).where(AgentMission.id != mid)).scalars().all() == []  # no child spawned


def test_steps_hold_structured_summaries_never_reasoning(kernel, tools) -> None:
    factory, actors = kernel
    leak = {"kind": "complete", "outcome": "done", "reasoning": "SECRET-THOUGHT: the student's password is hunter2"}
    runtime = build_runtime(tools, [TOOL, leak])
    mid = new_mission(runtime, factory, actors["a_student"])
    run(runtime, factory, actors["a_student"], mid)
    assert run(runtime, factory, actors["a_student"], mid).error_code == "INVALID_DECISION"
    mission, steps, _ = load(factory, actors["a_student"].organization_id, mid)
    assert set(steps[0].input_summary) == {"kind", "tool_name", "tool_input"}
    assert set(steps[0].output_summary) == {"ok", "data", "error_code"}
    with factory.open_tenant_session(mission.organization_id) as s:
        persisted = json.dumps([[st.input_summary, st.output_summary] for st in steps] + [mission.context] + [
            e.event_metadata for e in s.execute(select(OperationAuditEvent)).scalars()], default=str)
    assert "SECRET-THOUGHT" not in persisted and "hunter2" not in persisted and "reasoning" not in persisted


# --- Bounds -----------------------------------------------------------------------------------------------------


def test_max_steps_stops_the_mission(kernel, tools) -> None:
    factory, actors = kernel
    replan = AgentDecision(kind=DecisionKind.REPLAN, plan=["read notes", "solve past papers"])
    runtime = build_runtime(tools, [replan] * 10)
    mid = new_mission(runtime, factory, actors["a_student"], max_steps=2)
    with factory.open_tenant_session(actors["a_student"].organization_id) as s:
        results = runtime.run_until_blocked(s, actors["a_student"], mid, max_transitions=10)
    assert [r.status for r in results] == [AgentMissionStatus.RUNNING] * 2 + [AgentMissionStatus.FAILED]
    assert results[-1].error_code == "MAX_STEPS_EXCEEDED" and len(runtime.brain.seen) == 2
    mission, steps, audits = load(factory, actors["a_student"].organization_id, mid)
    assert mission.step_count == 2 and len(steps) == 2 and mission.current_plan["version"] == 2
    assert audits[-1] == "MISSION_FAILED"


def test_run_until_blocked_has_a_hard_cap(kernel, tools) -> None:
    factory, actors = kernel
    runtime = build_runtime(tools, [TOOL] * 50)
    mid = new_mission(runtime, factory, actors["a_student"], max_steps=100)
    with factory.open_tenant_session(actors["a_student"].organization_id) as s:
        with pytest.raises(AgentOSError) as exc:
            runtime.run_until_blocked(s, actors["a_student"], mid, max_transitions=HARD_TRANSITION_CAP + 1)
        assert exc.value.code == "INVALID_TRANSITION_LIMIT"
        assert len(runtime.run_until_blocked(s, actors["a_student"], mid, max_transitions=3)) == 3
    assert len(tools["lookup"].calls) == 3


# --- Terminal and waiting states -------------------------------------------------------------------------------


def test_complete_ends_the_mission(kernel, tools) -> None:
    factory, actors = kernel
    runtime = build_runtime(tools, [TOOL, COMPLETE, COMPLETE])
    mid = new_mission(runtime, factory, actors["a_student"])
    run(runtime, factory, actors["a_student"], mid)
    assert run(runtime, factory, actors["a_student"], mid).status == AgentMissionStatus.COMPLETED
    mission, _, audits = load(factory, actors["a_student"].organization_id, mid)
    assert mission.completed_at is not None and mission.context["outcome"] == "Revision plan ready."
    assert audits[-2:] == ["MISSION_COMPLETED", "AGENT_STEP_EXECUTED"]
    with pytest.raises(AgentOSError) as exc:
        run(runtime, factory, actors["a_student"], mid)
    assert exc.value.code == "MISSION_TERMINAL" and len(runtime.brain.seen) == 2


def test_wait_persists_and_resumes_only_on_its_event(kernel, tools) -> None:
    factory, actors = kernel
    now = [datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)]
    wait = AgentDecision(kind=DecisionKind.WAIT, wait_for=DomainEventType.ASSIGNMENT_SUBMITTED, wake_after_seconds=3600)
    runtime = build_runtime(tools, [wait, COMPLETE], clock=lambda: now[0])
    actor, org = actors["a_student"], actors["a_student"].organization_id
    mid = new_mission(runtime, factory, actor)
    assert run(runtime, factory, actor, mid).status == AgentMissionStatus.WAITING_EVENT
    mission, _, audits = load(factory, org, mid)
    assert (mission.waiting_for, mission.next_wake_at) == ("ASSIGNMENT_SUBMITTED", now[0] + timedelta(hours=1))
    assert "MISSION_WAITING" in audits

    idle = run(runtime, factory, actor, mid)  # nothing arrived: no transition, no brain call, no step
    assert (idle.transitioned, idle.status, len(runtime.brain.seen)) == (False, AgentMissionStatus.WAITING_EVENT, 1)
    with factory.open_tenant_session(org) as s:
        EventService().publish(s, event_type=DomainEventType.EXAM_STARTED, subject_type=SUBJECT_MISSION, subject_id=mid)
        event = EventService().publish(s, event_type="ASSIGNMENT_SUBMITTED", subject_type=SUBJECT_MISSION, subject_id=mid,
                                       payload={"assignment": "OS-A2"})
        s.commit()
        event_id = event.id
    assert run(runtime, factory, actor, mid).status == AgentMissionStatus.COMPLETED
    observation = runtime.brain.seen[1].observations[-1]
    assert (observation.kind, observation.source, observation.data["payload"]) == ("event", "ASSIGNMENT_SUBMITTED", {"assignment": "OS-A2"})
    with factory.open_tenant_session(org) as s:
        assert s.get(DomainEvent, event_id).consumed_at is not None
        assert len(EventService().pending(s, event_type="EXAM_STARTED")) == 1  # unrelated event untouched


def test_wait_for_network_uses_the_connectivity_state(kernel, tools) -> None:
    factory, actors = kernel
    runtime = build_runtime(tools, [AgentDecision(kind=DecisionKind.WAIT, wait_for=DomainEventType.NETWORK_RESTORED)])
    mid = new_mission(runtime, factory, actors["a_student"])
    assert run(runtime, factory, actors["a_student"], mid).status == AgentMissionStatus.WAITING_CONNECTIVITY


def test_ask_human_persists_the_waiting_state(kernel, tools) -> None:
    factory, actors = kernel
    ask = AgentDecision(kind=DecisionKind.ASK_HUMAN, question="Which exam date should I plan for?")
    runtime = build_runtime(tools, [ask])
    mid = new_mission(runtime, factory, actors["a_student"])
    assert run(runtime, factory, actors["a_student"], mid).status == AgentMissionStatus.WAITING_HUMAN
    mission, steps, audits = load(factory, actors["a_student"].organization_id, mid)
    assert mission.waiting_for == "HUMAN_RESPONDED" and mission.context["pending_question"] == ask.question
    assert steps[0].action_type == "ask_human" and audits[-2:] == ["MISSION_WAITING", "AGENT_STEP_EXECUTED"]


def test_allowed_delegation_spawns_a_child_and_the_parent_resumes_when_it_finishes(kernel, tools) -> None:
    factory, actors = kernel
    delegate = AgentDecision(kind=DecisionKind.DELEGATE, delegate_agent="reminder_agent", delegate_goal="Remind me daily")
    runtime = build_runtime(tools, [delegate, COMPLETE, COMPLETE])
    actor, org = actors["a_student"], actors["a_student"].organization_id
    parent = new_mission(runtime, factory, actor)
    assert run(runtime, factory, actor, parent).status == AgentMissionStatus.WAITING_EVENT
    mission, steps, audits = load(factory, org, parent)
    child = steps[0].output_summary["child_mission_id"]
    assert steps[0].delegated_agent == "reminder_agent" and "AGENT_DELEGATED" in audits
    assert run(runtime, factory, actor, child).status == AgentMissionStatus.COMPLETED  # the child, run by its owner
    assert run(runtime, factory, actor, parent).status == AgentMissionStatus.COMPLETED
    assert runtime.brain.seen[2].observations[-1].data["payload"]["child_mission_id"] == child


def test_a_brain_outage_changes_nothing(kernel, tools) -> None:
    factory, actors = kernel
    def outage(_context):
        raise BrainUnavailableError("down")
    runtime = build_runtime(tools, [outage])
    mid = new_mission(runtime, factory, actors["a_student"])
    result = run(runtime, factory, actors["a_student"], mid)
    assert (result.transitioned, result.error_code, result.status) == (False, "BRAIN_UNAVAILABLE", AgentMissionStatus.PENDING)
    mission, steps, audits = load(factory, actors["a_student"].organization_id, mid)
    assert (mission.status, steps, audits) == (AgentMissionStatus.PENDING, [], ["MISSION_CREATED"])


def test_cancel_is_audited_and_terminal(kernel, tools) -> None:
    factory, actors = kernel
    runtime = build_runtime(tools, [])
    mid = new_mission(runtime, factory, actors["a_student"])
    with factory.open_tenant_session(actors["a_admin"].organization_id) as s:  # an admin of the same organization may cancel
        assert runtime.cancel(s, actors["a_admin"], mid).status == AgentMissionStatus.CANCELLED
    _, _, audits = load(factory, actors["a_student"].organization_id, mid)
    assert audits[-1] == "MISSION_CANCELLED"


# --- Tenancy ----------------------------------------------------------------------------------------------------


def test_another_organization_cannot_read_run_or_cancel(kernel, tools) -> None:
    factory, actors = kernel
    runtime = build_runtime(tools, [TOOL])
    mid = new_mission(runtime, factory, actors["a_student"])
    for key in ("b_student", "b_admin"):
        actor = actors[key]
        with factory.open_tenant_session(actor.organization_id) as s:
            for call in (runtime.get_mission, runtime.list_steps, runtime.run_step, runtime.cancel):
                with pytest.raises(AgentOSError) as exc:
                    call(s, actor, mid)
                assert exc.value.code == "MISSION_NOT_FOUND"
        with factory.open_tenant_session(actors["a_student"].organization_id) as s:  # session of A, caller of B
            with pytest.raises(AgentOSError) as exc:
                runtime.get_mission(s, actor, mid)
            assert exc.value.code == "TENANT_MISMATCH"
    with factory.open_tenant_session(actors["a_admin"].organization_id) as s:  # same-org admin reads but never runs
        assert runtime.get_mission(s, actors["a_admin"], mid).id == mid
        with pytest.raises(AgentOSError):
            runtime.run_step(s, actors["a_admin"], mid)
    assert tools["lookup"].calls == []
    assert load(factory, actors["a_student"].organization_id, mid)[0].status == AgentMissionStatus.PENDING


def test_an_unbound_session_is_refused(kernel, tools) -> None:
    factory, actors = kernel
    with factory() as s:
        with pytest.raises(AgentOSError) as exc:
            build_runtime(tools, []).create_mission(s, actors["a_student"], CreateAgentMission(agent_key="study_planner", goal="x"))
    assert exc.value.code == "TENANT_MISMATCH"


def test_role_must_be_supported_by_the_agent(kernel, tools) -> None:
    factory, actors = kernel
    with factory.open_tenant_session(actors["a_admin"].organization_id) as s:
        with pytest.raises(AgentOSError) as exc:
            build_runtime(tools, []).create_mission(s, actors["a_admin"], CreateAgentMission(agent_key="study_planner", goal="x"))
    assert exc.value.code == "AGENT_ROLE_NOT_ALLOWED"


# --- Events and registries -----------------------------------------------------------------------------------


def test_domain_events_persist_redacted_and_allowlisted(kernel) -> None:
    factory, actors = kernel
    org = actors["a_student"].organization_id
    with factory.open_tenant_session(org) as s:
        event = EventService().publish(s, event_type=DomainEventType.ABSENCE_DETECTED, actor_account_id=actors["a_student"].account_id,
                                       subject_type="account", subject_id=actors["a_student"].account_id,
                                       payload={"course": "CS301", "guardian_phone": PHONE, "note": f"call {PHONE}", "token": "abc"})
        with pytest.raises(ValueError):
            EventService().publish(s, event_type="DROP_TABLES")
        s.commit()
        event_id = event.id
    with factory.open_tenant_session(org) as s:
        stored = s.get(DomainEvent, event_id)
        assert (stored.event_type, stored.subject_type, stored.subject_id, stored.consumed_at) == (
            "ABSENCE_DETECTED", "account", str(actors["a_student"].account_id), None)
        assert stored.organization_id == org and stored.created_at is not None
        assert stored.payload == {"course": "CS301", "guardian_phone": "[redacted]", "note": "call [redacted]", "token": "[redacted]"}
    with factory.open_tenant_session(actors["b_student"].organization_id) as s:
        assert s.get(DomainEvent, event_id) is None and EventService().pending(s) == []


def test_registries_refuse_unsafe_registrations(tools) -> None:
    class Writer(FakeCourseLookup):
        name, side_effecting = "fake_writer", True

    class LooseInput(BaseModel):
        course_code: str

    class Loose(FakeCourseLookup):
        name, input_model = "fake_loose", LooseInput

    class BadName(FakeCourseLookup):
        name = "os.system"

    registry = ToolRegistry()
    for tool in (Writer(), Loose(), BadName()):
        with pytest.raises(RegistryError):
            registry.register(tool)
    agents = AgentRegistry()
    with pytest.raises(RegistryError):
        agents.register(AgentSpec("app.agents:Evil", "x", supported_roles=frozenset({STUDENT})))
    with pytest.raises(RegistryError):
        agents.register(AgentSpec("no_roles", "x"))


# --- API ----------------------------------------------------------------------------------------------------------


def test_api_endpoints_follow_ownership_and_tenancy(app, client, tools) -> None:
    app.state.agent_runtime = build_runtime(tools, [TOOL])
    a_student = bearer(login(client, A_STUDENT)["access_token"])
    created = client.post("/agentos/missions", json={"agent_key": "study_planner", "goal": "Plan OS revision",
                                                     "organization_id": 999}, headers=a_student)
    assert created.status_code == 422  # extra fields (such as a client-chosen organization) are refused
    created = client.post("/agentos/missions", json={"agent_key": "study_planner", "goal": "Plan OS revision"}, headers=a_student)
    assert created.status_code == 201, created.text
    mid = created.json()["id"]
    assert client.post(f"/agentos/missions/{mid}/run-step", headers=a_student).json()["step_status"] == "executed"
    steps = client.get(f"/agentos/missions/{mid}/steps", headers=a_student).json()
    assert [s["tool_name"] for s in steps] == ["fake_course_lookup"]
    assert client.get(f"/agentos/missions/{mid}", headers=a_student).json()["step_count"] == 1

    for email in (B_STUDENT, B_ADMIN):
        other = bearer(login(client, email)["access_token"])
        assert client.get(f"/agentos/missions/{mid}", headers=other).status_code == 404
        assert client.get(f"/agentos/missions/{mid}/steps", headers=other).status_code == 404
        assert client.post(f"/agentos/missions/{mid}/run-step", headers=other).status_code == 404
        assert client.post(f"/agentos/missions/{mid}/cancel", headers=other).status_code == 404
    assert client.get(f"/agentos/missions/{mid}").status_code == 401

    assert client.post(f"/agentos/missions/{mid}/cancel", headers=a_student).json()["status"] == "cancelled"
    again = client.post(f"/agentos/missions/{mid}/cancel", headers=a_student)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "MISSION_TERMINAL"


def test_the_default_runtime_refuses_until_agents_are_registered(client) -> None:
    a_student = bearer(login(client, A_STUDENT)["access_token"])
    refused = client.post("/agentos/missions", json={"agent_key": "study_planner", "goal": "x"}, headers=a_student)
    assert refused.status_code == 400 and refused.json()["detail"]["code"] == "AGENT_NOT_REGISTERED"
