"""Phase 11 demo: an approval cannot outlive the conditions it was granted under.

Runs one registration mission against a *throwaway* seeded DB + Chroma store
(never the dev or demo DB -- it writes real rows) with the same five-agent
registry the FastAPI app uses and the offline mock LLM:

   1. Propose a conflict-free registration.
   2. Deterministic pre-check: VERIFIED.
   3. A human approves it (approval A1, bound to its payload fingerprint).
   4. An exam is rescheduled onto the event's slot.
   5. Resume.
   6. The execution-time recheck fails -- nothing is written.
   7. A1 becomes STALE (who approved and when is kept; why is recorded).
   8. The exam moves away again: the action is valid once more.
   9. Resume -> replan -> the action is re-verified against current data.
  10. A NEW approval A2 is required; A1 stays stale and cannot be revived.
  11. A human approves A2.
  12. It executes exactly once and the postcondition is verified.

Usage:
    python scripts/demo_phase11.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.api.main import _build_registry  # noqa: E402
from app.db.models.academic import Enrollment, Exam  # noqa: E402
from app.db.models.events import Event, EventRegistration  # noqa: E402
from app.db.models.identity import Student  # noqa: E402
from app.db.repositories.missions import get_latest_approval_for_step, list_approvals_for_step  # noqa: E402
from app.db.session import create_db_engine, create_session_factory, init_db  # noqa: E402
from app.graph.orchestrator import MissionOrchestrator  # noqa: E402
from app.llm.providers.mock import MockLLMProvider  # noqa: E402
from app.rag.config import get_rag_config  # noqa: E402
from app.rag.embeddings import DeterministicHashEmbedding  # noqa: E402
from app.rag.ingest import ingest_policy_directory  # noqa: E402
from app.rag.retriever import PolicyRetriever  # noqa: E402
from app.rag.vector_store import PolicyVectorStore  # noqa: E402
from app.schemas.enums import AgentName, ApprovalStatus, UserRole  # noqa: E402
from app.services.approval_gate import ApprovalGate, ApprovalGateError  # noqa: E402
from app.services.context import ContextService  # noqa: E402
from app.services.knowledge import KnowledgeService  # noqa: E402
from app.tools.build import build_default_tool_registry  # noqa: E402
from scripts.seed_data import run_seed  # noqa: E402

STUDENT = "STU-DEMO-001"
CLEAN_EVENT_ID = 10
GOAL = (
    "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my classes or "
    "exams, and register me for it."
)


def _build():
    temp_dir = Path(tempfile.mkdtemp(prefix="campusnexus-phase11-demo-"))
    engine = create_db_engine(db_path=temp_dir / "demo.db")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        run_seed(session)
    embedding = DeterministicHashEmbedding()
    store = PolicyVectorStore(path=temp_dir / "chroma", collection_name="phase11_demo", embedding_provider=embedding)
    ingest_policy_directory(get_rag_config().policy_dir, vector_store=store)
    knowledge = KnowledgeService(retriever=PolicyRetriever(vector_store=store, embedding_provider=embedding))
    llm = MockLLMProvider()
    registry = _build_registry(knowledge, llm, build_default_tool_registry())
    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm), session_factory, temp_dir


def _step(number: int, text: str) -> None:
    print(f"\n[{number:>2}] {text}")


def _approvals(session_factory, task_id: str) -> None:
    with session_factory() as session:
        for a in list_approvals_for_step(session, task_id):
            line = f"     {a.approval_id}  {a.status.value.upper():<9} fingerprint={(a.payload_fingerprint or '')[:16]}..."
            if a.decision_by:
                line += f"  approved/decided by {a.decision_by}"
            print(line)
            if a.status == ApprovalStatus.STALE:
                print(f"       expired at {a.invalidated_at.isoformat()}: {a.invalidation_reason}")


def _approve_latest(session_factory, task_id: str) -> str:
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
        return approval.approval_id


def _registrations(session_factory) -> int:
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == STUDENT)).scalar_one()
        return session.execute(
            select(func.count()).select_from(EventRegistration).where(
                EventRegistration.event_id == CLEAN_EVENT_ID, EventRegistration.student_id == student.id
            )
        ).scalar_one()


def _add_exam(session_factory) -> int:
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == STUDENT)).scalar_one()
        enrollment = session.execute(select(Enrollment).where(Enrollment.student_id == student.id)).scalars().first()
        event = session.get(Event, CLEAN_EVENT_ID)
        exam = Exam(course_id=enrollment.course_id, exam_type="quiz", scheduled_start=event.start_at,
                    scheduled_end=event.end_at, location="Rescheduled Exam Hall")
        session.add(exam)
        session.commit()
        print(f"     exam {exam.id} rescheduled onto '{event.title}' ({event.start_at.isoformat()})")
        return exam.id


def _remove_exam(session_factory, exam_id: int) -> None:
    with session_factory() as session:
        session.delete(session.get(Exam, exam_id))
        session.commit()


def main() -> int:
    orchestrator, session_factory, temp_dir = _build()
    print(f"Throwaway environment: {temp_dir}")

    _step(1, "Propose a conflict-free registration")
    final = orchestrator.run_mission(GOAL, user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT)
    task = next(t for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT)
    mission_id, task_id = final["mission_id"], task.task_id
    print(f"     mission {mission_id}: {final['mission_status'].value}")

    _step(2, "Deterministic pre-approval check")
    print(f"     precheck: {final['agent_results'][task_id].facts['precheck_status'].upper()}")

    _step(3, "Human approves (A1)")
    a1 = _approve_latest(session_factory, task_id)
    _approvals(session_factory, task_id)

    _step(4, "The world changes after approval")
    exam_id = _add_exam(session_factory)

    _step(5, "Resume")
    blocked = orchestrator.resume_mission(mission_id)
    print(f"     mission: {blocked['mission_status'].value}")

    _step(6, "Execution-time recheck")
    facts = blocked["agent_results"][task_id].facts
    print(f"     execution recheck: {str(facts.get('execution_recheck_status')).upper()}")
    print(f"     registrations written: {_registrations(session_factory)}")

    _step(7, "A1 is now STALE (not rejected)")
    _approvals(session_factory, task_id)
    with session_factory() as session:
        for e in ContextService(session).list_audit_events(mission_id):
            if e.event_type == "approval_invalidated":
                print(f"     audit approval_invalidated: {e.message}")

    _step(8, "The exam moves away; the action is valid again")
    _remove_exam(session_factory, exam_id)

    _step(9, "Resume -> replan -> re-verify against current data")
    replanned = orchestrator.resume_mission(mission_id)
    print(f"     mission: {replanned['mission_status'].value}")

    _step(10, "A NEW approval is required")
    _approvals(session_factory, task_id)
    with session_factory() as session:
        try:
            ApprovalGate(session).decide(a1, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
            print("     ERROR: stale approval was revived")
            return 1
        except ApprovalGateError as exc:
            print(f"     re-approving A1 refused: {exc}")

    _step(11, "Human approves the new request (A2)")
    a2 = _approve_latest(session_factory, task_id)
    print(f"     approved {a2}")

    _step(12, "Execute once + postcondition")
    done = orchestrator.resume_mission(mission_id)
    orchestrator.resume_mission(mission_id)  # a repeat resume must not write again
    facts = done["agent_results"][task_id].facts
    rows = _registrations(session_factory)
    print(f"     mission: {done['mission_status'].value}")
    print(f"     execution recheck: {str(facts.get('execution_recheck_status')).upper()}  "
          f"postcondition verified: {facts.get('postcondition_verified')}")
    print(f"     registrations written: {rows}")
    _approvals(session_factory, task_id)

    ok = (
        blocked["mission_status"].value == "failed"
        and replanned["mission_status"].value == "needs_approval"
        and done["mission_status"].value == "completed"
        and facts.get("postcondition_verified") is True
        and rows == 1
        and a1 != a2
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
