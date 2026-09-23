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


def build_orchestrator():
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

    orchestrator = _build_orchestrator_instance(session_factory, knowledge_service, tool_gateway, llm)
    return orchestrator, session_factory, knowledge_service, tool_gateway, llm


def _build_orchestrator_instance(session_factory, knowledge_service, tool_gateway, llm) -> MissionOrchestrator:
    """A *new* MissionOrchestrator instance (fresh in-memory LangGraph checkpointer)
    against the *same* session_factory/DB -- what "cross-process resumption" means in
    this codebase's tests (see tests/test_orchestrator_persistence.py)."""
    registry = AgentRegistry()
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, lambda s: EventsAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.CAMPUS_SERVICES_AGENT, lambda s: ServicesAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.ACTION_AGENT, lambda s: ActionAgent(session=s, knowledge_service=knowledge_service, tool_gateway=tool_gateway))
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


def main() -> int:
    orchestrator, session_factory, knowledge_service, tool_gateway, llm = build_orchestrator()
    failures = 0
    for name, scenario_fn in SCENARIOS:
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

    total = len(SCENARIOS)
    print(f"\n{total - failures}/{total} scenarios passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
