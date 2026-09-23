"""Phase 8 §11 regression tests: the Action Agent's execute-time recheck for
``register_event`` must independently re-derive preconditions against
*current* DB state -- never trust the propose-time snapshot -- and must
block execution if anything has changed since approval, not just on a hard
precheck failure.

Uses the real seeded DB. Event id 10 ("Competitive Coding Contest") is
genuinely conflict-free against STU-DEMO-001's timetable/exams at seed time
(verified directly against app.rules.event_availability); each test mutates
DB state *between* approval and execution to simulate the world changing
after a human already signed off.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.agents.action.agent import ActionAgent
from app.db.base import utc_now
from app.db.models.events import Event, EventRegistration, RegistrationStatus
from app.db.models.identity import Student
from app.db.repositories.missions import get_latest_approval_for_step
from app.schemas.agent import AgentMessage
from app.schemas.enums import AgentName, AgentResultStatus, ApprovalStatus, UserRole, VerificationStatus
from app.services.approval_gate import ApprovalGate
from app.services.context import ContextService
from app.tools.build import build_default_tool_registry

DEMO_STUDENT = "STU-DEMO-001"
CLEAN_EVENT_ID = 10  # Competitive Coding Contest -- open, not full, conflict-free at seed time
CLEAN_EVENT_TITLE = "Competitive Coding Contest"


def _setup_mission(session, mission_id: str, task_id: str):
    context = ContextService(session)
    context.create_mission(mission_id, DEMO_STUDENT, UserRole.STUDENT, "register for a workshop")
    context.create_mission_step(task_id, mission_id, AgentName.ACTION_AGENT, "register for a workshop")
    return context


def _agent(session, knowledge_service) -> ActionAgent:
    return ActionAgent(session=session, knowledge_service=knowledge_service, tool_gateway=build_default_tool_registry())


def _message(mission_id, task_id, *, constraints=None) -> AgentMessage:
    return AgentMessage(
        message_id="msg-1", mission_id=mission_id, task_id=task_id, source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACTION_AGENT, objective="register for a workshop",
        facts={"student_id": DEMO_STUDENT}, constraints=constraints or {},
    )


def _propose_and_approve(session, mission_id: str, task_id: str, knowledge_service, *, event_id=CLEAN_EVENT_ID, event_title=CLEAN_EVENT_TITLE) -> None:
    _setup_mission(session, mission_id, task_id)
    agent = _agent(session, knowledge_service)
    outcome = agent.handle(_message(mission_id, task_id, constraints={"tool_name": "register_event", "event_title": event_title}))
    assert outcome.verification.status in (VerificationStatus.VERIFIED, VerificationStatus.NEEDS_REVIEW), outcome.verification.issues
    approval = get_latest_approval_for_step(session, task_id)
    assert approval is not None and approval.status == ApprovalStatus.PENDING
    ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")


def _registration_count(session, event_id: int, student_code: str = DEMO_STUDENT) -> int:
    student = session.execute(select(Student).where(Student.student_code == student_code)).scalar_one()
    rows = session.execute(
        select(EventRegistration).where(EventRegistration.event_id == event_id, EventRegistration.student_id == student.id)
    ).scalars().all()
    return len(rows)


def test_clean_event_still_executes_successfully(seeded_session, knowledge_service) -> None:
    """Sanity baseline: an event genuinely free of conflicts, at capacity,
    and not a duplicate still executes normally -- the safety recheck must
    not become a false-positive blocker."""
    mission_id, task_id = "m-safety-1", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)

    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(mission_id, task_id, constraints={}))

    assert outcome.agent_result.status == AgentResultStatus.SUCCESS
    assert outcome.verification.status == VerificationStatus.VERIFIED
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 1


def test_real_conflict_blocks_registration_even_though_propose_time_missed_it(seeded_session, knowledge_service) -> None:
    """The core regression this phase fixes: "Tech Talk: Cloud Native
    Systems" (event_id=6) genuinely overlaps the student's CS301 class.
    Proposing it directly (bypassing an EventsAgent dependency, so no
    upstream conflict data is available) must still be caught at
    execute-time by the Action Agent's own independent recheck -- it must
    never rely solely on an (absent or wrong) upstream claim."""
    mission_id, task_id = "m-safety-2", "t-1"
    _setup_mission(seeded_session, mission_id, task_id)
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message(mission_id, task_id, constraints={"tool_name": "register_event", "event_title": "Tech Talk: Cloud Native Systems"}))

    approval = get_latest_approval_for_step(seeded_session, task_id)
    assert approval is not None
    ApprovalGate(seeded_session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    outcome = agent.handle(_message(mission_id, task_id, constraints={}))

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert outcome.verification.status == VerificationStatus.FAILED
    assert any("longer valid" in e for e in outcome.agent_result.errors)
    assert _registration_count(seeded_session, 6) == 0


def test_capacity_fills_between_approval_and_execution(seeded_session, knowledge_service) -> None:
    mission_id, task_id = "m-safety-3", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)

    # Simulate the event filling up after approval but before execution --
    # lowering capacity to the current confirmed count is equivalent to (and
    # far simpler than) registering enough other students to fill it, and
    # doesn't depend on how many students happen to be seeded.
    event = seeded_session.get(Event, CLEAN_EVENT_ID)
    confirmed_count = len(
        seeded_session.execute(
            select(EventRegistration).where(
                EventRegistration.event_id == event.id, EventRegistration.status == RegistrationStatus.CONFIRMED
            )
        ).scalars().all()
    )
    event.capacity = confirmed_count
    seeded_session.commit()

    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(mission_id, task_id, constraints={}))

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert any("longer valid" in e for e in outcome.agent_result.errors)
    assert any("capacity" in e.lower() for e in outcome.agent_result.errors)
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 0


def test_deadline_passes_between_approval_and_execution(seeded_session, knowledge_service) -> None:
    mission_id, task_id = "m-safety-4", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)

    event = seeded_session.get(Event, CLEAN_EVENT_ID)
    event.registration_deadline = utc_now() - timedelta(hours=1)
    seeded_session.commit()

    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(mission_id, task_id, constraints={}))

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert any("longer valid" in e for e in outcome.agent_result.errors)
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 0


def test_duplicate_appears_between_approval_and_execution(seeded_session, knowledge_service) -> None:
    """Another process/mission registers the student for the same event
    after this proposal was approved but before it executed -- the recheck's
    not_already_registered check (a hard check) must block this cleanly
    rather than attempt a second write."""
    mission_id, task_id = "m-safety-5", "t-1"
    _propose_and_approve(seeded_session, mission_id, task_id, knowledge_service)

    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    seeded_session.add(EventRegistration(event_id=CLEAN_EVENT_ID, student_id=student.id, status=RegistrationStatus.CONFIRMED))
    seeded_session.commit()

    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(mission_id, task_id, constraints={}))

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert any("longer valid" in e for e in outcome.agent_result.errors)
    # Still exactly one row -- the other process's registration -- never a second one.
    assert _registration_count(seeded_session, CLEAN_EVENT_ID) == 1
