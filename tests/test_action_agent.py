"""End-to-end tests for the ActionAgent (real seeded DB + real RAG fixtures,
real ToolGateway -- no live LLM, no network).

Every test drives ``ActionAgent.handle()`` directly against a real
``Mission``/``MissionStep`` (created via ContextService, exactly as the
Mission Orchestrator would before ever dispatching a task -- ``ToolCallRecord``
has a real FK to ``mission_steps``), which is the same pattern
``tests/test_orchestrator_persistence.py`` uses to construct mission state
directly rather than always going through a full ``run_mission`` call.
"""
from __future__ import annotations

from sqlalchemy import select

from app.agents.action.agent import ActionAgent
from app.db.models.events import EventRegistration
from app.db.models.identity import Student
from app.schemas.agent import AgentMessage
from app.schemas.enums import AgentName, AgentResultStatus, ApprovalStatus, UserRole, VerificationStatus
from app.services.approval_gate import ApprovalGate
from app.services.context import ContextService
from app.tools.build import build_default_tool_registry

DEMO_STUDENT = "STU-DEMO-001"


def _setup_mission(session, mission_id: str, task_id: str, objective: str = "do the thing"):
    context = ContextService(session)
    context.create_mission(mission_id, DEMO_STUDENT, UserRole.STUDENT, objective)
    context.create_mission_step(task_id, mission_id, AgentName.ACTION_AGENT, objective)
    return context


def _agent(session, knowledge_service, tool_gateway=None) -> ActionAgent:
    return ActionAgent(session=session, knowledge_service=knowledge_service, tool_gateway=tool_gateway or build_default_tool_registry())


def _message(mission_id, task_id, *, student_id=DEMO_STUDENT, constraints=None, facts_extra=None) -> AgentMessage:
    facts = {"student_id": student_id}
    facts.update(facts_extra or {})
    return AgentMessage(
        message_id="msg-1", mission_id=mission_id, task_id=task_id, source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACTION_AGENT, objective="do the thing", facts=facts, constraints=constraints or {},
    )


# ---------------------------------------------------------------------------
# Propose (first dispatch)
# ---------------------------------------------------------------------------


def test_unsupported_tool_name_fails_without_creating_an_approval(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-1", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message("m-1", "t-1", constraints={"tool_name": "not_a_real_tool"}))

    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert outcome.verification.status == VerificationStatus.FAILED

    from app.db.repositories.missions import get_latest_approval_for_step

    assert get_latest_approval_for_step(seeded_session, "t-1") is None


def test_valid_registration_proposal_pauses_for_approval(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-2", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(
        _message("m-2", "t-1", constraints={"tool_name": "register_event", "event_title": "Tech Talk: Cloud Native Systems"})
    )

    assert outcome.agent_result.status == AgentResultStatus.PARTIAL
    assert outcome.verification.status == VerificationStatus.NEEDS_REVIEW
    assert outcome.agent_result.facts["awaiting_approval"] is True

    from app.db.repositories.missions import get_latest_approval_for_step

    approval = get_latest_approval_for_step(seeded_session, "t-1")
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING

    # Never executed at propose time -- only the pre-existing AI workshop registration exists.
    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    rows = seeded_session.execute(select(EventRegistration).where(EventRegistration.student_id == student.id)).scalars().all()
    assert len(rows) == 1


def test_duplicate_registration_fails_precheck_no_approval_created(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-3", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(
        _message(
            "m-3", "t-1",
            constraints={"tool_name": "register_event", "event_title": "Artificial Intelligence & Deep Learning Workshop"},
        )
    )
    assert outcome.verification.status == VerificationStatus.FAILED
    assert any("already has a registration" in i for i in outcome.verification.issues)

    from app.db.repositories.missions import get_latest_approval_for_step

    assert get_latest_approval_for_step(seeded_session, "t-1") is None


def test_full_event_fails_precheck(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-4", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message("m-4", "t-1", constraints={"tool_name": "register_event", "event_title": "Startup Pitch Night"}))
    assert outcome.verification.status == VerificationStatus.FAILED
    assert any("capacity" in i for i in outcome.verification.issues)


def test_invalid_student_identity_fails_precheck(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-5", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(
        _message(
            "m-5", "t-1", student_id="STU-NOPE-999",
            constraints={"tool_name": "register_event", "event_title": "Tech Talk: Cloud Native Systems"},
        )
    )
    assert outcome.verification.status == VerificationStatus.FAILED
    assert any("No student record" in i for i in outcome.verification.issues)


def test_calendar_creation_proposal(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-6", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(
        _message(
            "m-6", "t-1",
            constraints={
                "tool_name": "create_calendar_event", "title": "Prep for Cloud Native Talk",
                "source_event_title": "Tech Talk: Cloud Native Systems", "lead_time_hours": 24, "duration_hours": 1,
            },
        )
    )
    assert outcome.verification.status == VerificationStatus.NEEDS_REVIEW
    assert outcome.agent_result.facts["proposal"]["parameters"]["source_type"] == "event"


def test_case_creation_invalid_category_fails_precheck(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-7", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(
        _message("m-7", "t-1", constraints={"tool_name": "create_campus_case", "category": "parking", "description": "x"})
    )
    assert outcome.verification.status == VerificationStatus.FAILED


def test_case_creation_valid_proposal(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-8", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(
        _message(
            "m-8", "t-1",
            constraints={"tool_name": "create_campus_case", "category": "hostel", "description": "Leaking tap.", "priority": "normal"},
        )
    )
    assert outcome.verification.status == VerificationStatus.NEEDS_REVIEW
    assert outcome.agent_result.facts["proposal"]["parameters"]["department"] == "Hostel Office"


# ---------------------------------------------------------------------------
# Still pending / rejected (defensive re-dispatch guards)
# ---------------------------------------------------------------------------


def test_redispatch_while_still_pending_never_executes(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-9", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message("m-9", "t-1", constraints={"tool_name": "register_event", "event_title": "Tech Talk: Cloud Native Systems"}))

    outcome = agent.handle(_message("m-9", "t-1", constraints={}))  # dispatched again before any decision
    assert outcome.agent_result.status == AgentResultStatus.PARTIAL
    assert outcome.agent_result.facts["awaiting_approval"] is True

    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    rows = seeded_session.execute(select(EventRegistration).where(EventRegistration.student_id == student.id)).scalars().all()
    assert len(rows) == 1  # only the pre-existing AI workshop registration -- nothing new


def test_redispatch_after_rejection_never_executes(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-10", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message("m-10", "t-1", constraints={"tool_name": "register_event", "event_title": "Tech Talk: Cloud Native Systems"}))

    from app.db.repositories.missions import get_latest_approval_for_step

    approval = get_latest_approval_for_step(seeded_session, "t-1")
    ApprovalGate(seeded_session).decide(approval.approval_id, decision=ApprovalStatus.REJECTED, decision_by="admin-demo")

    outcome = agent.handle(_message("m-10", "t-1", constraints={}))
    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert outcome.verification.status == VerificationStatus.FAILED

    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    rows = seeded_session.execute(
        select(EventRegistration).where(EventRegistration.student_id == student.id, EventRegistration.event_id == 6)
    ).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# Execute (second dispatch, after approval)
# ---------------------------------------------------------------------------


def test_approved_registration_executes_and_verifies(seeded_session, knowledge_service) -> None:
    # "Competitive Coding Contest" (event_id=10) is genuinely conflict-free
    # against STU-DEMO-001's timetable/exams -- unlike "Tech Talk: Cloud
    # Native Systems" (event_id=6), which really does overlap CS301 (see
    # tests/test_action_safety.py) and is used there deliberately instead.
    _setup_mission(seeded_session, "m-11", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message("m-11", "t-1", constraints={"tool_name": "register_event", "event_title": "Competitive Coding Contest"}))

    from app.db.repositories.missions import get_latest_approval_for_step

    approval = get_latest_approval_for_step(seeded_session, "t-1")
    ApprovalGate(seeded_session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    outcome = agent.handle(_message("m-11", "t-1", constraints={}))
    assert outcome.agent_result.status == AgentResultStatus.SUCCESS
    assert outcome.verification.status == VerificationStatus.VERIFIED
    assert outcome.agent_result.facts["postcondition_verified"] is True

    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    rows = seeded_session.execute(
        select(EventRegistration).where(EventRegistration.student_id == student.id, EventRegistration.event_id == 10)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status.value == "confirmed"


def test_duplicate_execution_via_redispatch_never_double_registers(seeded_session, knowledge_service) -> None:
    """A resume_mission called twice (e.g. an operator double-clicking
    "resume") must never create a second registration -- the second
    dispatch finds the ToolCallRecord already SUCCESS and re-verifies
    without re-executing."""
    _setup_mission(seeded_session, "m-12", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message("m-12", "t-1", constraints={"tool_name": "register_event", "event_title": "Competitive Coding Contest"}))

    from app.db.repositories.missions import get_latest_approval_for_step

    approval = get_latest_approval_for_step(seeded_session, "t-1")
    ApprovalGate(seeded_session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    outcome1 = agent.handle(_message("m-12", "t-1", constraints={}))
    outcome2 = agent.handle(_message("m-12", "t-1", constraints={}))

    assert outcome1.agent_result.status == AgentResultStatus.SUCCESS
    assert outcome2.agent_result.status == AgentResultStatus.SUCCESS
    assert outcome2.agent_result.facts["already_executed"] is True

    student = seeded_session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
    rows = seeded_session.execute(
        select(EventRegistration).where(EventRegistration.student_id == student.id, EventRegistration.event_id == 10)
    ).scalars().all()
    assert len(rows) == 1


def test_case_creation_execute_persists_and_verifies(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-13", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(
        _message(
            "m-13", "t-1",
            constraints={"tool_name": "create_campus_case", "category": "facilities", "description": "AC broken.", "priority": "normal"},
        )
    )

    from app.db.repositories.missions import get_latest_approval_for_step

    approval = get_latest_approval_for_step(seeded_session, "t-1")
    ApprovalGate(seeded_session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    outcome = agent.handle(_message("m-13", "t-1", constraints={}))
    assert outcome.agent_result.status == AgentResultStatus.SUCCESS
    case_code = outcome.agent_result.facts["tool_result"]["case_code"]

    from app.db.repositories.cases import get_case, get_case_sla

    sla = get_case_sla(seeded_session, case_code)
    assert sla is not None
    assert (sla.response_due_at - sla.resolution_due_at) is not None  # both set
    assert sla.response_due_at < sla.resolution_due_at

    case = get_case(seeded_session, case_code)
    assert case is not None
    assert case.student.student_code == DEMO_STUDENT
    assert case.status.value == "open"


def test_calendar_creation_execute_persists_and_verifies(seeded_session, knowledge_service) -> None:
    _setup_mission(seeded_session, "m-14", "t-1")
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(
        _message(
            "m-14", "t-1",
            constraints={
                "tool_name": "create_calendar_event", "title": "Prep for Cloud Native Talk",
                "source_event_title": "Tech Talk: Cloud Native Systems", "lead_time_hours": 24, "duration_hours": 1,
            },
        )
    )
    from app.db.repositories.missions import get_latest_approval_for_step

    approval = get_latest_approval_for_step(seeded_session, "t-1")
    ApprovalGate(seeded_session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")

    outcome = agent.handle(_message("m-14", "t-1", constraints={}))
    assert outcome.agent_result.status == AgentResultStatus.SUCCESS

    from datetime import timedelta

    from app.db.repositories.calendar import get_calendar_entry
    from app.services import events as events_service

    source_event = events_service.get_event_summary_by_title(seeded_session, "Tech Talk: Cloud Native Systems")
    expected_start = source_event.start_at - timedelta(hours=24)
    entry = get_calendar_entry(seeded_session, DEMO_STUDENT, "Prep for Cloud Native Talk", expected_start)
    assert entry is not None
    assert entry.source_type == "event"
    assert entry.source_id == source_event.event_id
