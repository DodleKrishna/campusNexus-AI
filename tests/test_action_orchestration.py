"""Mission-level pause -> ApprovalGate.decide -> resume tests, integrating
the Action Agent into the real LangGraph Mission Orchestrator with zero
changes to app/graph/orchestrator.py, scheduler.py, or validator.py -- the
same NEEDS_REVIEW/BLOCKED pause mechanism Phase 5/6 already use and test.

The cross-process test constructs a **brand-new** MissionOrchestrator
instance (a fresh in-memory LangGraph checkpointer, no shared state with
whatever ran the mission before) to resume, mirroring
tests/test_orchestrator_persistence.py's existing convention for proving
resumption survives a real process restart.
"""
from __future__ import annotations

from app.agents.action.agent import ActionAgent
from app.agents.events.agent import EventsAgent
from app.agents.services.agent import ServicesAgent
from app.db.repositories.missions import get_latest_approval_for_step
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.providers.mock import MockLLMProvider
from app.schemas.enums import AgentName, ApprovalStatus, MissionStatus, TaskStatus, UserRole
from app.services.approval_gate import ApprovalGate
from app.services.context import ContextService
from app.tools.build import build_default_tool_registry

DEMO_STUDENT = "STU-DEMO-001"

REGISTRATION_GOAL = (
    "Find the workshop titled 'Tech Talk: Cloud Native Systems', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
CASE_GOAL = "File a complaint: 'Hostel washroom tap still leaking after last repair.'"
REJECTED_CASE_GOAL = "File a complaint: 'Library projector bulb needs replacement urgently.'"


def _build_registry(session_factory, knowledge_service, llm, tool_gateway):
    registry = AgentRegistry()
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, lambda s: EventsAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.CAMPUS_SERVICES_AGENT, lambda s: ServicesAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.ACTION_AGENT, lambda s: ActionAgent(session=s, knowledge_service=knowledge_service, tool_gateway=tool_gateway))
    return registry


def _orchestrator(session_factory, knowledge_service, tool_gateway):
    llm = MockLLMProvider()
    registry = _build_registry(session_factory, knowledge_service, llm, tool_gateway)
    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm)


def test_mission_pauses_for_approval_and_completes_after_approve_and_resume(seeded_session, session_factory, knowledge_service) -> None:
    tool_gateway = build_default_tool_registry()
    orchestrator = _orchestrator(session_factory, knowledge_service, tool_gateway)

    final = orchestrator.run_mission(REGISTRATION_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    assert final["mission_status"] == MissionStatus.NEEDS_APPROVAL

    action_task_id = next(t.task_id for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT)
    assert final["task_status"][action_task_id] == TaskStatus.BLOCKED

    with session_factory() as session:
        approval = get_latest_approval_for_step(session, action_task_id)
        assert approval.status == ApprovalStatus.PENDING
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    final2 = orchestrator.resume_mission(final["mission_id"])
    assert final2["mission_status"] == MissionStatus.COMPLETED
    assert final2["task_status"][action_task_id] == TaskStatus.COMPLETED

    with session_factory() as session:
        context = ContextService(session)
        event_types = [e.event_type for e in context.list_audit_events(final["mission_id"])]
        for expected in ("approval_requested", "approval_approved", "mission_resumed"):
            assert expected in event_types


def test_mission_never_executes_after_rejection(seeded_session, session_factory, knowledge_service) -> None:
    tool_gateway = build_default_tool_registry()
    orchestrator = _orchestrator(session_factory, knowledge_service, tool_gateway)

    final = orchestrator.run_mission(REJECTED_CASE_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    action_task_id = next(t.task_id for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT)

    with session_factory() as session:
        approval = get_latest_approval_for_step(session, action_task_id)
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.REJECTED, decision_by="admin-demo", decision_reason="not needed")

    final2 = orchestrator.resume_mission(final["mission_id"])
    assert final2["mission_status"] == MissionStatus.FAILED
    assert final2["task_status"][action_task_id] == TaskStatus.FAILED


def test_cross_process_approval_and_resumption(seeded_session, session_factory, knowledge_service) -> None:
    """Approve using one ApprovalGate/session, then resume using a *second,
    independently-constructed* MissionOrchestrator instance -- proving
    resumption depends only on the Context Service (SQLite), never on the
    first orchestrator's in-memory LangGraph checkpointer."""
    tool_gateway = build_default_tool_registry()
    orchestrator_a = _orchestrator(session_factory, knowledge_service, tool_gateway)

    final = orchestrator_a.run_mission(CASE_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    assert final["mission_status"] == MissionStatus.NEEDS_APPROVAL
    action_task_id = next(t.task_id for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT)

    with session_factory() as session:
        approval = get_latest_approval_for_step(session, action_task_id)
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    # A brand-new orchestrator instance -- no shared Python object with orchestrator_a.
    orchestrator_b = _orchestrator(session_factory, knowledge_service, tool_gateway)
    final2 = orchestrator_b.resume_mission(final["mission_id"])

    assert final2["mission_status"] == MissionStatus.COMPLETED
    with session_factory() as session:
        context = ContextService(session)
        runs = context.list_agent_runs(final["mission_id"])
        action_runs = [r for r in runs if r.agent == AgentName.ACTION_AGENT]
        assert len(action_runs) == 2  # propose (partial) + execute (success)
        assert action_runs[-1].status.value == "success"
