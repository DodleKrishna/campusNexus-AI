"""Deterministic action evaluation (no live LLM, no network).

Mirrors eval/multiagent_scenarios.py's PASS/FAIL structure: runs the *real*
Action Agent (not a test double) and real Tool Gateway, with MockLLMProvider
so results are fully deterministic. Every check reads *persisted DB state*
and *structured VerificationResult/ApprovalRecord* data -- never a substring
match against free text.

Unlike the Phase 6 evals (read-only, safe to rerun against the shared dev
DB), this eval *writes* real rows -- a registration, calendar entries, cases
-- so it builds its own throwaway SQLite DB + Chroma store per run (the same
isolation strategy tests/conftest.py already uses), never the shared
data/campusnexus.db/data/chroma. This is what keeps the eval deterministic
and safely rerunnable rather than degrading after its first execution.

Usage:
    python eval/action_scenarios.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.agents.academic.agent import AcademicAgent
from app.agents.action.agent import ActionAgent
from app.agents.events.agent import EventsAgent
from app.agents.services.agent import ServicesAgent
from app.db.repositories.cases import get_case
from app.db.repositories.events import get_registration
from app.db.repositories.missions import get_latest_approval_for_step
from app.db.session import create_db_engine, create_session_factory, init_db
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.providers.mock import MockLLMProvider
from app.rag.config import get_rag_config
from app.rag.embeddings import DeterministicHashEmbedding
from app.rag.ingest import ingest_policy_directory
from app.rag.retriever import PolicyRetriever
from app.rag.vector_store import PolicyVectorStore
from app.schemas.enums import AgentName, ApprovalStatus, MissionStatus, TaskStatus, UserRole
from app.services.approval_gate import ApprovalGate
from app.services.context import ContextService
from app.services.knowledge import KnowledgeService
from app.tools.build import build_default_tool_registry
from scripts.seed_data import run_seed

DEMO_STUDENT = "STU-DEMO-001"
UNKNOWN_STUDENT = "STU-NOPE-999"

# "Competitive Coding Contest" is genuinely conflict-free against
# STU-DEMO-001's timetable/exams -- unlike "Tech Talk: Cloud Native
# Systems", which really does overlap CS301 (Phase 8 §11 safety scenario,
# see scenario_schedule_conflict_blocks_execution below).
REGISTER_GOAL = (
    "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
CONFLICTING_REGISTER_GOAL = (
    "Find the workshop titled 'Tech Talk: Cloud Native Systems', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
DUPLICATE_REGISTER_GOAL = (
    "Find the workshop titled 'Artificial Intelligence & Deep Learning Workshop', verify there are no "
    "conflicts with my classes or exams, and register me for it."
)
FULL_EVENT_GOAL = (
    "Find the workshop titled 'Startup Pitch Night', verify there are no conflicts with my classes or "
    "exams, and register me for it."
)
CASE_GOAL_A = "File a complaint: 'Hostel corridor light not working.'"
CASE_GOAL_REJECTED = "File a complaint: 'Library projector bulb needs replacement.'"


def build_orchestrator(*, with_academic: bool = False):
    """``with_academic=False`` is the original Phase 7/8 registry (no Academic
    Agent, so a registration's pre-approval conflict check is honestly "not
    checked" and only the execute-time recheck can catch a clash).
    ``with_academic=True`` is the full production registry the Phase 10
    scenarios need (Academic timetable/exams feeding the proposal)."""
    temp_dir = Path(tempfile.mkdtemp(prefix="campusnexus-action-eval-"))
    engine = create_db_engine(db_path=temp_dir / "eval.db")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as setup_session:
        run_seed(setup_session)

    config = get_rag_config()
    embedding_provider = DeterministicHashEmbedding()
    vector_store = PolicyVectorStore(path=temp_dir / "chroma", collection_name="eval_policies", embedding_provider=embedding_provider)
    ingest_policy_directory(config.policy_dir, vector_store=vector_store)
    retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
    knowledge_service = KnowledgeService(retriever=retriever)

    llm = MockLLMProvider()
    tool_gateway = build_default_tool_registry()

    orchestrator = _build_orchestrator_instance(session_factory, knowledge_service, tool_gateway, llm, with_academic=with_academic)
    return orchestrator, session_factory, knowledge_service, tool_gateway, llm


def _build_orchestrator_instance(session_factory, knowledge_service, tool_gateway, llm, *, with_academic: bool = False) -> MissionOrchestrator:
    """A *new* MissionOrchestrator instance (fresh in-memory LangGraph checkpointer)
    against the *same* session_factory/DB -- what "cross-process resumption" means in
    this codebase's tests (see tests/test_orchestrator_persistence.py)."""
    registry = AgentRegistry()
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, lambda s: EventsAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.CAMPUS_SERVICES_AGENT, lambda s: ServicesAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.ACTION_AGENT, lambda s: ActionAgent(session=s, knowledge_service=knowledge_service, tool_gateway=tool_gateway))
    if with_academic:
        registry.register(AgentName.ACADEMIC_AGENT, lambda s: AcademicAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm)


def _action_task_id(final) -> str:
    return next(t.task_id for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT)


def _approve(session_factory, task_id: str) -> None:
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-eval")


def _reject(session_factory, task_id: str, reason: str) -> None:
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.REJECTED, decision_by="admin-eval", decision_reason=reason)


def scenario_successful_registration_persists_and_verifies(orchestrator, session_factory, knowledge_service=None, tool_gateway=None, llm=None) -> Dict[str, Any]:
    final = orchestrator.run_mission(REGISTER_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    paused = final["mission_status"] == MissionStatus.NEEDS_APPROVAL
    task_id = _action_task_id(final)
    _approve(session_factory, task_id)
    final2 = orchestrator.resume_mission(final["mission_id"])

    with session_factory() as session:
        reg = get_registration(session, 10, DEMO_STUDENT)  # event 10 = Competitive Coding Contest
    passed = (
        paused
        and final2["mission_status"] == MissionStatus.COMPLETED
        and reg is not None
        and reg.status.value == "confirmed"
        and final2["agent_results"][task_id].facts["postcondition_verified"] is True
    )
    return {"passed": passed, "final_status": final2["mission_status"].value, "registration_status": reg.status.value if reg else None}


def scenario_rejected_action_never_executes(orchestrator, session_factory, knowledge_service=None, tool_gateway=None, llm=None) -> Dict[str, Any]:
    final = orchestrator.run_mission(CASE_GOAL_REJECTED, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task_id = _action_task_id(final)
    _reject(session_factory, task_id, "duplicate ticket")
    final2 = orchestrator.resume_mission(final["mission_id"])

    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
    passed = (
        final2["mission_status"] == MissionStatus.FAILED
        and final2["task_status"][task_id] == TaskStatus.FAILED
        and approval.status == ApprovalStatus.REJECTED
    )
    return {"passed": passed, "final_status": final2["mission_status"].value}


def scenario_unauthorized_ownership_mismatch_is_rejected(orchestrator, session_factory, knowledge_service=None, tool_gateway=None, llm=None) -> Dict[str, Any]:
    from app.schemas.enums import ToolAccessType
    from app.schemas.tools import ToolCall
    from app.tools.build import build_default_tool_registry as _build

    gateway = _build()
    call = ToolCall(
        tool_call_id="tc-eval-unauth", mission_id="m-eval", task_id="t-eval", tool_name="register_event",
        arguments={"student_id": "STU2023002", "event_id": 6}, read_or_write=ToolAccessType.WRITE,
        requires_approval=True, idempotency_key="k-eval-unauth",
    )
    with session_factory() as session:
        result = gateway.execute(session, call, caller_role=UserRole.STUDENT, caller_student_id=DEMO_STUDENT)
    passed = result.status.value == "failed" and "ownership" in (result.error or "")
    return {"passed": passed, "error": result.error}


def scenario_duplicate_registration_precheck_fails_before_approval(orchestrator, session_factory, knowledge_service=None, tool_gateway=None, llm=None) -> Dict[str, Any]:
    final = orchestrator.run_mission(DUPLICATE_REGISTER_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task_id = _action_task_id(final)
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
    passed = final["mission_status"] == MissionStatus.FAILED and approval is None
    return {"passed": passed, "final_status": final["mission_status"].value}


def scenario_full_event_precheck_fails_before_approval(orchestrator, session_factory, knowledge_service=None, tool_gateway=None, llm=None) -> Dict[str, Any]:
    final = orchestrator.run_mission(FULL_EVENT_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task_id = _action_task_id(final)
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
    passed = final["mission_status"] == MissionStatus.FAILED and approval is None
    return {"passed": passed, "final_status": final["mission_status"].value}


def scenario_invalid_student_identity_never_reaches_approval(orchestrator, session_factory, knowledge_service=None, tool_gateway=None, llm=None) -> Dict[str, Any]:
    final = orchestrator.run_mission(REGISTER_GOAL, user_id=UNKNOWN_STUDENT, user_role=UserRole.STUDENT, student_id=UNKNOWN_STUDENT)
    passed = final["mission_status"] == MissionStatus.FAILED
    return {"passed": passed, "final_status": final["mission_status"].value}


def scenario_interrupted_then_resumed_completes_exactly_once(orchestrator, session_factory, knowledge_service=None, tool_gateway=None, llm=None) -> Dict[str, Any]:
    """A fresh MissionOrchestrator instance (same DB, brand-new in-memory
    checkpointer) resumes and executes; a second resume_mission call
    afterward must never double-execute (idempotent)."""
    final = orchestrator.run_mission(CASE_GOAL_A, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task_id = _action_task_id(final)
    _approve(session_factory, task_id)

    fresh_orchestrator = _build_orchestrator_instance(session_factory, knowledge_service, tool_gateway, llm)
    final2 = fresh_orchestrator.resume_mission(final["mission_id"])
    case_code = final2["agent_results"][task_id].facts["tool_result"]["case_code"]

    # Idempotent re-resume: the step is already COMPLETED, so scheduling
    # never re-dispatches it -- the mission is simply already-complete.
    final3 = fresh_orchestrator.resume_mission(final["mission_id"])

    with session_factory() as session:
        case = get_case(session, case_code)
        case_found = case is not None
        case_open = case is not None and case.status.value == "open"
        case_owner = case.student.student_code if case is not None else None
    passed = (
        final2["mission_status"] == MissionStatus.COMPLETED
        and final3["mission_status"] == MissionStatus.COMPLETED
        and case_found
        and case_open
        and case_owner == DEMO_STUDENT
    )
    return {"passed": passed, "case_code": case_code}


def scenario_schedule_conflict_blocks_execution_after_approval(orchestrator, session_factory, knowledge_service=None, tool_gateway=None, llm=None) -> Dict[str, Any]:
    """Phase 8 §11: a genuine schedule conflict (Tech Talk: Cloud Native
    Systems really does overlap CS301) must block execution even though it
    was already approved -- proving the execute-time recheck independently
    re-derives conflicts rather than trusting the propose-time snapshot."""
    final = orchestrator.run_mission(CONFLICTING_REGISTER_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task_id = _action_task_id(final)
    _approve(session_factory, task_id)
    final2 = orchestrator.resume_mission(final["mission_id"])

    with session_factory() as session:
        reg = get_registration(session, 6, DEMO_STUDENT)  # event 6 = Tech Talk: Cloud Native Systems
    passed = (
        final2["mission_status"] == MissionStatus.FAILED
        and reg is None
        and any("longer valid" in e for e in final2["agent_results"][task_id].errors)
    )
    return {"passed": passed, "final_status": final2["mission_status"].value, "errors": final2["agent_results"][task_id].errors}


# ---------------------------------------------------------------------------
# Phase 10 -- pre-approval conflict detection, TOCTOU, duplicate-failure
# suppression. Run against a *separate* throwaway environment with the full
# registry (Academic Agent included). The TOCTOU scenario must run before the
# conflict-free registration (both use event 10); it removes the exam it adds.
# ---------------------------------------------------------------------------

CLEAN_EVENT_ID = 10  # Competitive Coding Contest
CONFLICT_EVENT_ID = 6  # Tech Talk: Cloud Native Systems


def _action_runs(session_factory, mission_id: str):
    with session_factory() as session:
        return [r for r in ContextService(session).list_agent_runs(mission_id) if r.agent == AgentName.ACTION_AGENT]


def _audit_types(session_factory, mission_id: str):
    with session_factory() as session:
        return [e.event_type for e in ContextService(session).list_audit_events(mission_id)]


def _registration_plan_is_parallel(final) -> bool:
    tasks = final["plan"].tasks
    action = next(t for t in tasks if t.agent == AgentName.ACTION_AGENT)
    upstream = [t for t in tasks if t.task_id in action.dependencies]
    agents = sorted(t.agent.value for t in upstream)
    return agents == ["academic_agent", "academic_agent", "events_opportunity_agent"] and all(not t.dependencies for t in upstream)


def _add_exam_over_event(session_factory, event_id: int) -> int:
    """Simulates an exam being rescheduled onto the event's slot."""
    from sqlalchemy import select

    from app.db.models.academic import Enrollment, Exam
    from app.db.models.events import Event
    from app.db.models.identity import Student

    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
        enrollment = session.execute(select(Enrollment).where(Enrollment.student_id == student.id)).scalars().first()
        event = session.get(Event, event_id)
        exam = Exam(course_id=enrollment.course_id, exam_type="quiz", scheduled_start=event.start_at,
                    scheduled_end=event.end_at, location="Rescheduled Exam Hall")
        session.add(exam)
        session.commit()
        return exam.id


def _remove_exam(session_factory, exam_id: int) -> None:
    from app.db.models.academic import Exam

    with session_factory() as session:
        session.delete(session.get(Exam, exam_id))
        session.commit()


def scenario_toctou_schedule_change_after_approval_blocks_write(orchestrator, session_factory, **_) -> Dict[str, Any]:
    final = orchestrator.run_mission(REGISTER_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task_id = _action_task_id(final)
    precheck = final["agent_results"][task_id].facts.get("precheck_status")
    _approve(session_factory, task_id)
    exam_id = _add_exam_over_event(session_factory, CLEAN_EVENT_ID)  # the schedule changes after approval
    try:
        final2 = orchestrator.resume_mission(final["mission_id"])
        with session_factory() as session:
            reg = get_registration(session, CLEAN_EVENT_ID, DEMO_STUDENT)
        result = final2["agent_results"][task_id]
    finally:
        _remove_exam(session_factory, exam_id)
    passed = (
        final["mission_status"] == MissionStatus.NEEDS_APPROVAL
        and precheck == "verified"
        and final2["mission_status"] == MissionStatus.FAILED
        and result.facts.get("execution_recheck_status") == "failed"
        and any("longer valid" in e for e in result.errors)
        and reg is None
    )
    return {"passed": passed, "precheck_at_approval": precheck, "final_status": final2["mission_status"].value,
            "execution_recheck": result.facts.get("execution_recheck_status"), "registration_row": reg is not None}


def scenario_conflict_free_registration_verified_before_approval(orchestrator, session_factory, **_) -> Dict[str, Any]:
    final = orchestrator.run_mission(REGISTER_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task_id = _action_task_id(final)
    proposal_facts = final["agent_results"][task_id].facts
    schedule = proposal_facts["proposal"]["supporting_facts"]["schedule_check"]
    _approve(session_factory, task_id)
    final2 = orchestrator.resume_mission(final["mission_id"])
    executed = final2["agent_results"][task_id].facts
    with session_factory() as session:
        reg = get_registration(session, CLEAN_EVENT_ID, DEMO_STUDENT)
    passed = (
        _registration_plan_is_parallel(final)
        and final["mission_status"] == MissionStatus.NEEDS_APPROVAL
        and proposal_facts["precheck_status"] == "verified"
        and schedule["performed"] and schedule["source"] == "upstream_academic_tasks"
        and final2["mission_status"] == MissionStatus.COMPLETED
        and executed["execution_recheck_status"] == "verified"
        and executed["postcondition_verified"] is True
        and reg is not None and reg.status.value == "confirmed"
    )
    return {"passed": passed, "precheck_at_approval": proposal_facts["precheck_status"],
            "execution_recheck": executed.get("execution_recheck_status"), "final_status": final2["mission_status"].value}


def scenario_known_conflict_blocked_before_approval(orchestrator, session_factory, **_) -> Dict[str, Any]:
    final = orchestrator.run_mission(CONFLICTING_REGISTER_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task_id = _action_task_id(final)
    facts = final["agent_results"][task_id].facts
    schedule = facts.get("schedule_check") or {}
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
        reg = get_registration(session, CONFLICT_EVENT_ID, DEMO_STUDENT)
    passed = (
        final["mission_status"] == MissionStatus.FAILED
        and approval is None
        and reg is None
        and facts.get("precheck_status") == "failed"
        and schedule.get("performed") is True
        and len(schedule.get("timetable_conflicts", [])) + len(schedule.get("exam_conflicts", [])) > 0
    )
    return {"passed": passed, "final_status": final["mission_status"].value, "approval_created": approval is not None,
            "conflicts": schedule.get("timetable_conflicts")}


def scenario_duplicate_deterministic_failure_is_not_replanned_twice(orchestrator, session_factory, **_) -> Dict[str, Any]:
    final = orchestrator.run_mission(FULL_EVENT_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    runs = _action_runs(session_factory, final["mission_id"])
    events = _audit_types(session_factory, final["mission_id"])
    passed = (
        final["mission_status"] == MissionStatus.FAILED
        and len(runs) == 2  # original + one replan attempt, never a third identical run
        and events.count("replan_triggered") == 1
        and events.count("duplicate_failure_detected") == 1
        and final["final_result"].startswith("Execution stopped because the same verified failure occurred again")
    )
    return {"passed": passed, "action_runs": len(runs), "replans": events.count("replan_triggered"),
            "duplicate_detected": events.count("duplicate_failure_detected")}


PHASE10_SCENARIOS = [
    ("toctou-schedule-change-after-approval-blocks-write", scenario_toctou_schedule_change_after_approval_blocks_write),
    ("conflict-free-registration-verified-before-approval", scenario_conflict_free_registration_verified_before_approval),
    ("known-conflict-blocked-before-approval", scenario_known_conflict_blocked_before_approval),
    ("duplicate-deterministic-failure-is-not-replanned-twice", scenario_duplicate_deterministic_failure_is_not_replanned_twice),
]


# ---------------------------------------------------------------------------
# Phase 11 -- approval lifecycle hardening. A third throwaway environment
# (full registry). Every scenario targets event 10; the first two restore the
# state they change, and only the last one actually registers.
# ---------------------------------------------------------------------------


def _step_approvals(session_factory, task_id: str):
    from app.db.repositories.missions import list_approvals_for_step

    with session_factory() as session:
        return [(a.approval_id, a.status, a.decision_by, a.invalidation_reason) for a in list_approvals_for_step(session, task_id)]


def _invalidation_events(session_factory, mission_id: str):
    with session_factory() as session:
        return [dict(e.event_metadata or {}) for e in ContextService(session).list_audit_events(mission_id)
                if e.event_type == "approval_invalidated"]


def _approve_and_break(orchestrator, session_factory, mutate):
    """Run a conflict-free registration to approval, approve it, apply ``mutate``
    (returns an undo callable), resume. Returns (final, resumed, task_id, undo)."""
    final = orchestrator.run_mission(REGISTER_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task_id = _action_task_id(final)
    _approve(session_factory, task_id)
    undo = mutate()
    resumed = orchestrator.resume_mission(final["mission_id"])
    return final, resumed, task_id, undo


def _stale_checks(session_factory, final, resumed, task_id) -> Dict[str, Any]:
    chain = _step_approvals(session_factory, task_id)
    events = _invalidation_events(session_factory, final["mission_id"])
    with session_factory() as session:
        reg = get_registration(session, CLEAN_EVENT_ID, DEMO_STUDENT)
    ok = (
        final["mission_status"] == MissionStatus.NEEDS_APPROVAL
        and resumed["mission_status"] == MissionStatus.FAILED
        and [status for _, status, _, _ in chain] == [ApprovalStatus.STALE]
        and chain[0][2] == "admin-eval"  # the human approval is still on record
        and len(events) == 1 and events[0]["approved_by"] == "admin-eval"
        and reg is None
    )
    return {"ok": ok, "approvals": [s.value for _, s, _, _ in chain], "reason": chain[0][3] if chain else None,
            "registration_row": reg is not None}


def scenario_approval_invalidated_by_schedule_change(orchestrator, session_factory, **_) -> Dict[str, Any]:
    def mutate():
        exam_id = _add_exam_over_event(session_factory, CLEAN_EVENT_ID)
        return lambda: _remove_exam(session_factory, exam_id)

    final, resumed, task_id, undo = _approve_and_break(orchestrator, session_factory, mutate)
    try:
        checks = _stale_checks(session_factory, final, resumed, task_id)
    finally:
        undo()
    passed = checks.pop("ok") and "schedule conflict" in (checks["reason"] or "")
    return {"passed": passed, **checks}


def scenario_approval_invalidated_by_capacity_change(orchestrator, session_factory, **_) -> Dict[str, Any]:
    from sqlalchemy import func, select

    from app.db.models.events import Event, EventRegistration, RegistrationStatus

    def mutate():
        with session_factory() as session:
            event = session.get(Event, CLEAN_EVENT_ID)
            original = event.capacity
            # Full: capacity drops to the current confirmed count (as if other students took the seats).
            event.capacity = session.execute(
                select(func.count()).select_from(EventRegistration).where(
                    EventRegistration.event_id == CLEAN_EVENT_ID, EventRegistration.status == RegistrationStatus.CONFIRMED
                )
            ).scalar_one()
            session.commit()

        def undo():
            with session_factory() as session:
                session.get(Event, CLEAN_EVENT_ID).capacity = original
                session.commit()

        return undo

    final, resumed, task_id, undo = _approve_and_break(orchestrator, session_factory, mutate)
    try:
        checks = _stale_checks(session_factory, final, resumed, task_id)
    finally:
        undo()
    passed = checks.pop("ok") and "capacity" in (checks["reason"] or "").lower()
    return {"passed": passed, **checks}


def scenario_stale_approval_requires_new_approval(orchestrator, session_factory, **_) -> Dict[str, Any]:
    holder: Dict[str, int] = {}

    def mutate():
        holder["exam_id"] = _add_exam_over_event(session_factory, CLEAN_EVENT_ID)
        return lambda: _remove_exam(session_factory, holder["exam_id"])

    final, blocked, task_id, restore = _approve_and_break(orchestrator, session_factory, mutate)
    stale_id = _step_approvals(session_factory, task_id)[0][0]
    restore()  # the exam moves away: the action is valid again
    replanned = orchestrator.resume_mission(final["mission_id"])
    chain = _step_approvals(session_factory, task_id)

    # The stale approval can never be revived -- even though the action is valid again.
    with session_factory() as session:
        try:
            ApprovalGate(session).decide(stale_id, decision=ApprovalStatus.APPROVED, decision_by="admin-eval")
            revived = True
        except Exception:  # noqa: BLE001 -- ApprovalGateError is the expected outcome
            revived = False

    _approve(session_factory, task_id)  # decides the NEW (latest) request
    done = orchestrator.resume_mission(final["mission_id"])
    orchestrator.resume_mission(final["mission_id"])  # a repeat resume must not write again
    from sqlalchemy import func, select

    from app.db.models.events import EventRegistration
    from app.db.models.identity import Student

    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
        rows = session.execute(
            select(func.count()).select_from(EventRegistration).where(
                EventRegistration.event_id == CLEAN_EVENT_ID, EventRegistration.student_id == student.id
            )
        ).scalar_one()
        final_chain = [(a_id, status) for a_id, status, _, _ in _step_approvals(session_factory, task_id)]
    passed = (
        blocked["mission_status"] == MissionStatus.FAILED
        and replanned["mission_status"] == MissionStatus.NEEDS_APPROVAL
        and [status for _, status, _, _ in chain] == [ApprovalStatus.STALE, ApprovalStatus.PENDING]
        and not revived
        and done["mission_status"] == MissionStatus.COMPLETED
        and done["agent_results"][task_id].facts.get("postcondition_verified") is True
        and final_chain == [(stale_id, ApprovalStatus.STALE), (chain[1][0], ApprovalStatus.APPROVED)]
        and rows == 1
    )
    return {"passed": passed, "approvals": [f"{a_id}:{s.value}" for a_id, s in final_chain],
            "stale_revived": revived, "final_status": done["mission_status"].value, "registration_rows": rows}


PHASE11_SCENARIOS = [
    ("approval-invalidated-by-schedule-change", scenario_approval_invalidated_by_schedule_change),
    ("approval-invalidated-by-capacity-change", scenario_approval_invalidated_by_capacity_change),
    ("stale-approval-requires-new-approval", scenario_stale_approval_requires_new_approval),
]


SCENARIOS = [
    ("successful-registration-persists-and-verifies", scenario_successful_registration_persists_and_verifies),
    ("rejected-action-never-executes", scenario_rejected_action_never_executes),
    ("unauthorized-ownership-mismatch-is-rejected", scenario_unauthorized_ownership_mismatch_is_rejected),
    ("duplicate-registration-precheck-fails-before-approval", scenario_duplicate_registration_precheck_fails_before_approval),
    ("full-event-precheck-fails-before-approval", scenario_full_event_precheck_fails_before_approval),
    ("invalid-student-identity-never-reaches-approval", scenario_invalid_student_identity_never_reaches_approval),
    ("interrupted-then-resumed-completes-exactly-once", scenario_interrupted_then_resumed_completes_exactly_once),
    ("schedule-conflict-blocks-execution-after-approval", scenario_schedule_conflict_blocks_execution_after_approval),
]


def _run_scenarios(scenarios, *, with_academic: bool) -> int:
    orchestrator, session_factory, knowledge_service, tool_gateway, llm = build_orchestrator(with_academic=with_academic)
    failures = 0
    for name, scenario_fn in scenarios:
        try:
            result = scenario_fn(orchestrator, session_factory, knowledge_service=knowledge_service, tool_gateway=tool_gateway, llm=llm)
        except Exception as exc:  # noqa: BLE001 -- a scenario raising is itself a FAIL, not a crash of the runner
            result = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        status = "PASS" if result.get("passed") else "FAIL"
        if not result.get("passed"):
            failures += 1
        print(f"{status} {name}")
        for key, value in result.items():
            if key != "passed":
                print(f"     {key}: {value}")
    return failures


def main() -> int:
    failures = _run_scenarios(SCENARIOS, with_academic=False)
    failures += _run_scenarios(PHASE10_SCENARIOS, with_academic=True)
    failures += _run_scenarios(PHASE11_SCENARIOS, with_academic=True)

    total = len(SCENARIOS) + len(PHASE10_SCENARIOS) + len(PHASE11_SCENARIOS)
    print(f"\n{total - failures}/{total} scenarios passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
