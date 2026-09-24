"""Phase 13 demo: AI recommends, the student selects, an approver approves.

Runs one mission against a *throwaway* seeded DB + Chroma store (never the dev
or demo DB -- it writes real rows) with the five-agent registry the FastAPI
app uses and the offline mock LLM:

   1. Goal names no event: "Find a suitable event, check my schedule and
      prepare my registration."
   2. Planning + discovery: Events, timetable and exam tasks run; nothing is
      proposed and no approval exists.
   3. Candidates are checked deterministically against the verified timetable
      and exams: safe and unsafe choices, each with its reason.
   4. The student tries an unsafe event: refused, no approval.
   5. The student selects "Competitive Coding Contest" (USER_SELECTION).
   6. The same mission continues: proposal, pre-check VERIFIED, one approval.
   7. Selecting it again changes nothing (idempotent).
   8. An administrator approves.
   9. The student resumes: execute-time recheck, register_event, postcondition.
  10. Registration rows before/after: exactly one new row.

Only step 1 plans with the (mock) LLM; everything after the discovery is
deterministic. Exits non-zero on any deviation.

Usage:
    python scripts/demo_phase13.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.api.main import _build_registry  # noqa: E402
from app.db.models.events import EventRegistration  # noqa: E402
from app.db.models.identity import Student  # noqa: E402
from app.db.models.mission import AgentRun, ApprovalRecord, ToolCallRecord  # noqa: E402
from app.db.repositories.missions import get_latest_approval_for_step  # noqa: E402
from app.db.session import create_db_engine, create_session_factory, init_db  # noqa: E402
from app.graph.orchestrator import MissionOrchestrator, user_selection_required  # noqa: E402
from app.llm.providers.mock import MockLLMProvider  # noqa: E402
from app.rag.config import get_rag_config  # noqa: E402
from app.rag.embeddings import DeterministicHashEmbedding  # noqa: E402
from app.rag.ingest import ingest_policy_directory  # noqa: E402
from app.rag.retriever import PolicyRetriever  # noqa: E402
from app.rag.vector_store import PolicyVectorStore  # noqa: E402
from app.schemas.enums import ApprovalStatus, MissionStatus, UserRole  # noqa: E402
from app.schemas.selection import SelectionResult  # noqa: E402
from app.services import target_selection  # noqa: E402
from app.services.approval_gate import ApprovalGate  # noqa: E402
from app.services.context import ContextService  # noqa: E402
from app.services.knowledge import KnowledgeService  # noqa: E402
from app.tools.build import build_default_tool_registry  # noqa: E402
from scripts.seed_data import run_seed  # noqa: E402

STUDENT = "STU-DEMO-001"
GOAL = "Find a suitable event, check my schedule and prepare my registration."
CHOSEN_EVENT_ID = 10  # Competitive Coding Contest
UNSAFE_EVENT_ID = 2  # Hackathon Kickoff Session (clashes with a class)


class _CountingLLM(MockLLMProvider):
    """The mock LLM, counting calls so the demo can show selection needs none."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def __getattribute__(self, name: str):
        attr = super().__getattribute__(name)
        if callable(attr) and not name.startswith("_"):
            def counted(*args, **kwargs):
                object.__setattr__(self, "count", super(_CountingLLM, self).__getattribute__("count") + 1)
                return attr(*args, **kwargs)
            return counted
        return attr


def _build():
    temp_dir = Path(tempfile.mkdtemp(prefix="campusnexus-phase13-demo-"))
    engine = create_db_engine(db_path=temp_dir / "demo.db")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        run_seed(session)
    embedding = DeterministicHashEmbedding()
    store = PolicyVectorStore(path=temp_dir / "chroma", collection_name="phase13_demo", embedding_provider=embedding)
    ingest_policy_directory(get_rag_config().policy_dir, vector_store=store)
    knowledge = KnowledgeService(retriever=PolicyRetriever(vector_store=store, embedding_provider=embedding))
    llm = _CountingLLM()
    registry = _build_registry(knowledge, llm, build_default_tool_registry())
    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm), session_factory, llm


def _step(number: int, text: str) -> None:
    print(f"\n[{number:>2}] {text}")


def _count(session_factory, model, mission_id: str) -> int:
    with session_factory() as session:
        return session.execute(select(func.count()).select_from(model).where(model.mission_id == mission_id)).scalar_one()


def _registrations(session_factory) -> int:
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == STUDENT)).scalar_one()
        return session.execute(
            select(func.count()).select_from(EventRegistration).where(
                EventRegistration.event_id == CHOSEN_EVENT_ID, EventRegistration.student_id == student.id
            )
        ).scalar_one()


def _require(condition: bool, message: str) -> None:
    if not condition:
        print(f"\nRESULT: FAIL -- {message}")
        raise SystemExit(1)


def main() -> None:
    orchestrator, session_factory, llm = _build()
    rows_before = _registrations(session_factory)

    _step(1, f"Goal: {GOAL!r}")
    final = orchestrator.run_mission(GOAL, user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT)
    mission_id = final["mission_id"]
    plan = final["plan"]
    print(f"     mission {mission_id} -> {final['mission_status'].value}")
    for task in plan.tasks:
        print(f"       {task.task_id.rsplit('-', 1)[-1]}  {task.agent.value:<26} deps={[d.rsplit('-', 1)[-1] for d in task.dependencies]}  {task.objective}")
    print(f"     selection_required_actions={plan.selection_required_actions}")

    _step(2, "Discovery finished: no action proposed, no approval")
    required = user_selection_required(plan, GOAL)
    print(f"     user_selection_required={required}  approvals={_count(session_factory, ApprovalRecord, mission_id)}  "
          f"tool_calls={_count(session_factory, ToolCallRecord, mission_id)}")
    _require(final["mission_status"] == MissionStatus.COMPLETED and required, "discovery did not end awaiting a selection")
    _require(_count(session_factory, ApprovalRecord, mission_id) == 0, "an approval exists before any selection")
    llm_calls_after_discovery = llm.count

    _step(3, "Candidates, checked deterministically against the verified timetable + exams")
    with session_factory() as session:
        candidates = target_selection.list_candidates(session, mission_id)
    for c in sorted(candidates, key=lambda c: (not c.selectable, c.resource_id)):
        why = "" if c.selectable else f" -- {' '.join(c.assessment.reasons)}"
        print(f"     [{'SELECTABLE' if c.selectable else 'blocked   '}] #{c.resource_id:<3} {c.title:<48} {c.assessment.status.value}{why}")
    _require(any(c.resource_id == CHOSEN_EVENT_ID and c.selectable for c in candidates), "the demo event is not selectable")

    _step(4, f"Student tries an unsafe event (#{UNSAFE_EVENT_ID})")
    blocked = orchestrator.select_target(
        mission_id, tool_name="register_event", resource_type="event", resource_id=UNSAFE_EVENT_ID, selected_by=STUDENT
    )
    print(f"     {blocked.result.value.upper()}: {blocked.message}")
    _require(blocked.result == SelectionResult.BLOCKED, "an unsafe event was accepted")
    _require(_count(session_factory, ApprovalRecord, mission_id) == 0, "a blocked selection created an approval")

    _step(5, "Student selects 'Competitive Coding Contest'")
    outcome = orchestrator.select_target(
        mission_id, tool_name="register_event", resource_type="event", resource_id=CHOSEN_EVENT_ID, selected_by=STUDENT
    )
    print(f"     {outcome.result.value.upper()}: {outcome.message}  (selection {outcome.selection.selection_id})")
    _require(outcome.result == SelectionResult.SELECTED, "the selection was not accepted")

    _step(6, "Same mission continues: proposal -> pre-check -> approval request")
    with session_factory() as session:
        context = ContextService(session)
        mission = context.get_mission(mission_id)
        selection = context.get_active_target_selection(mission_id, outcome.selection.step_id)
        run = session.execute(select(AgentRun).where(AgentRun.step_id == selection.step_id)).scalars().all()[-1]
        approval = get_latest_approval_for_step(session, selection.step_id)
        status, fingerprint = mission.status, approval.payload_fingerprint
    provenance = run.facts["target_provenance"]["source"]
    print(f"     mission {mission_id} -> {status.value}; step {selection.step_id.rsplit('-', 1)[-1]}")
    print(f"     target provenance={provenance}  pre-check={run.facts['precheck_status']}  "
          f"approval {approval.approval_id} {approval.status.value} fingerprint={fingerprint[:16]}...")
    _require(status == MissionStatus.NEEDS_APPROVAL, "the mission is not waiting for approval")
    _require(provenance == "user_selection" and run.facts["precheck_status"] == "verified", "wrong provenance or pre-check")

    _step(7, "Student clicks Select again")
    again = orchestrator.select_target(
        mission_id, tool_name="register_event", resource_type="event", resource_id=CHOSEN_EVENT_ID, selected_by=STUDENT
    )
    approvals = _count(session_factory, ApprovalRecord, mission_id)
    print(f"     {again.result.value.upper()}: {again.message}  approvals={approvals}")
    _require(again.result == SelectionResult.ALREADY_SELECTED and approvals == 1, "a repeated selection duplicated work")
    print(f"     registration rows so far: {_registrations(session_factory) - rows_before} (selecting never writes)")

    _step(8, "Administrator approves")
    with session_factory() as session:
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
    print(f"     {approval.approval_id} APPROVED by admin-demo")

    _step(9, "Student resumes: execute-time recheck -> register_event -> postcondition")
    resumed = orchestrator.resume_mission(mission_id)
    facts = resumed["agent_results"][selection.step_id].facts
    print(f"     mission -> {resumed['mission_status'].value}; execution recheck={facts.get('execution_recheck_status')}  "
          f"postcondition_verified={facts.get('postcondition_verified')}")
    _require(resumed["mission_status"] == MissionStatus.COMPLETED and facts.get("postcondition_verified"), "execution did not verify")

    _step(10, "Database")
    rows_after = _registrations(session_factory)
    print(f"     registrations for event #{CHOSEN_EVENT_ID}: before={rows_before} after={rows_after}")
    print(f"     LLM calls after discovery: {llm.count - llm_calls_after_discovery}")
    _require(rows_after == rows_before + 1, "expected exactly one new registration")
    _require(llm.count == llm_calls_after_discovery, "selection/continuation called the LLM")
    with session_factory() as session:
        print("     audit trail:")
        for event in ContextService(session).list_audit_events(mission_id):
            print(f"       - {event.event_type}: {event.message[:110]}")

    print("\nRESULT: PASS")


if __name__ == "__main__":
    main()
