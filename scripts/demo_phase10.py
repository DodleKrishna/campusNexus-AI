"""Phase 10 demo: mission & action flow refinement, end-to-end.

Runs four missions against a *throwaway* seeded DB + Chroma store (never the
dev or demo DB -- these write real rows) with the same five-agent registry the
FastAPI app uses and the offline mock LLM:

  A. Conflict-free registration: Academic context before approval, pre-check
     VERIFIED, approve, resume, execute-time recheck VERIFIED, one row,
     postcondition VERIFIED.
  B. Known conflicting registration: conflict found before approval, no
     approval created, no write.
  C. TOCTOU: VERIFIED at approval, exam rescheduled onto the event after
     approval, execution blocked, zero rows.
  D. Deterministic failure: the same failure is not run a third time.

For each: the plan with its dependency trace, the dispatch rounds, the
pre-check / execute-time verdicts, DB effects and the audit trail.

Usage:
    python scripts/demo_phase10.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from app.api.main import _build_registry  # noqa: E402
from app.db.models.academic import Enrollment, Exam  # noqa: E402
from app.db.models.events import Event, EventRegistration  # noqa: E402
from app.db.models.identity import Student  # noqa: E402
from app.db.repositories.missions import get_latest_approval_for_step  # noqa: E402
from app.db.session import create_db_engine, create_session_factory, init_db  # noqa: E402
from app.graph.orchestrator import MissionOrchestrator  # noqa: E402
from app.llm.providers.mock import MockLLMProvider  # noqa: E402
from app.rag.config import get_rag_config  # noqa: E402
from app.rag.embeddings import DeterministicHashEmbedding  # noqa: E402
from app.rag.ingest import ingest_policy_directory  # noqa: E402
from app.rag.retriever import PolicyRetriever  # noqa: E402
from app.rag.vector_store import PolicyVectorStore  # noqa: E402
from app.schemas.enums import AgentName, ApprovalStatus, UserRole  # noqa: E402
from app.services.approval_gate import ApprovalGate  # noqa: E402
from app.services.context import ContextService  # noqa: E402
from app.services.knowledge import KnowledgeService  # noqa: E402
from app.tools.build import build_default_tool_registry  # noqa: E402
from scripts.seed_data import run_seed  # noqa: E402

STUDENT = "STU-DEMO-001"
CLEAN_EVENT_ID = 10


def _goal(title: str) -> str:
    return (
        f"Find the workshop titled '{title}', verify there are no conflicts with my classes or exams, "
        "and register me for it."
    )


def _banner(title: str) -> None:
    print(f"\n{'=' * 14} {title} {'=' * 14}")


def _build():
    temp_dir = Path(tempfile.mkdtemp(prefix="campusnexus-phase10-demo-"))
    engine = create_db_engine(db_path=temp_dir / "demo.db")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        run_seed(session)
    embedding = DeterministicHashEmbedding()
    store = PolicyVectorStore(path=temp_dir / "chroma", collection_name="phase10_demo", embedding_provider=embedding)
    ingest_policy_directory(get_rag_config().policy_dir, vector_store=store)
    knowledge = KnowledgeService(retriever=PolicyRetriever(vector_store=store, embedding_provider=embedding))
    llm = MockLLMProvider()
    registry = _build_registry(knowledge, llm, build_default_tool_registry())
    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm), session_factory, temp_dir


def _print_plan(final) -> None:
    tasks = final["plan"].tasks
    short = {t.task_id: f"T{i}" for i, t in enumerate(tasks, start=1)}
    print("Plan (dependency trace):")
    for t in tasks:
        deps = ", ".join(short[d] for d in t.dependencies) or "none -- runs in the first parallel round"
        print(f"  {short[t.task_id]} {t.agent.value:<26} {t.objective!r}  <- depends on: {deps}")
    # Dispatch rounds, derived from the dependency graph (what the scheduler runs together).
    level: dict = {}
    for t in tasks:  # plan order is topological for these plans
        level[t.task_id] = 1 + max((level[d] for d in t.dependencies), default=0)
    for round_no in sorted(set(level.values())):
        print(f"  dispatch round {round_no}: {', '.join(short[t] for t, lv in level.items() if lv == round_no)}")


def _action(final):
    return next(t for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT)


def _print_verdicts(final, label: str) -> None:
    task = _action(final)
    result = final["agent_results"].get(task.task_id)
    if result is None:
        print(f"{label}: action task never dispatched")
        return
    facts = result.facts
    schedule = facts.get("schedule_check") or (facts.get("proposal") or {}).get("supporting_facts", {}).get("schedule_check")
    print(f"{label}: mission={final['mission_status'].value}")
    if "precheck_status" in facts:
        print(f"  pre-approval check: {facts['precheck_status'].upper()}")
    if schedule:
        print(
            f"  schedule context: performed={schedule['performed']} source={schedule['source']} "
            f"classes={schedule.get('timetable_entries_checked')} exams={schedule.get('exam_entries_checked')} "
            f"conflicts={schedule['timetable_conflicts'] + schedule['exam_conflicts']}"
        )
    if "execution_recheck_status" in facts:
        print(f"  execution-time recheck: {str(facts['execution_recheck_status']).upper()}")
    if "postcondition_verified" in facts:
        print(f"  postcondition verified: {facts['postcondition_verified']}")
    if result.errors:
        print(f"  issues: {result.errors}")


def _registrations(session_factory, event_id: int) -> int:
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == STUDENT)).scalar_one()
        return len(session.execute(select(EventRegistration).where(
            EventRegistration.event_id == event_id, EventRegistration.student_id == student.id)).scalars().all())


def _approve(session_factory, task_id: str) -> None:
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
        print(f"Human approved {approval.approval_id}.")


def _print_audit(session_factory, mission_id: str) -> None:
    with session_factory() as session:
        context = ContextService(session)
        runs = context.list_agent_runs(mission_id)
        events = context.list_audit_events(mission_id)
    print(f"Agent runs: {len(runs)} ({', '.join(r.agent.value + ':' + r.status.value for r in runs)})")
    print("Audit trail:")
    for e in events:
        extra = ""
        meta = e.event_metadata or {}
        if "failure_fingerprint" in meta:
            extra = f"  [fingerprint {meta['failure_fingerprint']}, repeat={meta['repeat_of_earlier_failure']}]"
        print(f"  - {e.event_type}: {e.message[:130]}{extra}")


def main() -> int:
    orchestrator, session_factory, temp_dir = _build()
    print(f"Throwaway environment: {temp_dir}")

    _banner("C. TOCTOU: schedule changes after approval")
    final = orchestrator.run_mission(_goal("Competitive Coding Contest"), user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT)
    _print_plan(final)
    _print_verdicts(final, "After first run")
    _approve(session_factory, _action(final).task_id)
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == STUDENT)).scalar_one()
        enrollment = session.execute(select(Enrollment).where(Enrollment.student_id == student.id)).scalars().first()
        event = session.get(Event, CLEAN_EVENT_ID)
        exam = Exam(course_id=enrollment.course_id, exam_type="quiz", scheduled_start=event.start_at,
                    scheduled_end=event.end_at, location="Rescheduled Exam Hall")
        session.add(exam)
        session.commit()
        exam_id = exam.id
        print(f"Schedule changed AFTER approval: a quiz was rescheduled onto {event.title!r} ({event.start_at}).")
    final2 = orchestrator.resume_mission(final["mission_id"])
    _print_verdicts(final2, "After resume")
    print(f"Registration rows for event {CLEAN_EVENT_ID}: {_registrations(session_factory, CLEAN_EVENT_ID)}")
    _print_audit(session_factory, final["mission_id"])
    with session_factory() as session:  # undo, so scenario A can use the same event
        session.delete(session.get(Exam, exam_id))
        session.commit()

    _banner("A. Conflict-free registration")
    final = orchestrator.run_mission(_goal("Competitive Coding Contest"), user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT)
    _print_plan(final)
    _print_verdicts(final, "After first run (paused for approval)")
    _approve(session_factory, _action(final).task_id)
    final2 = orchestrator.resume_mission(final["mission_id"])
    _print_verdicts(final2, "After resume")
    print(f"Registration rows for event {CLEAN_EVENT_ID}: {_registrations(session_factory, CLEAN_EVENT_ID)}")
    _print_audit(session_factory, final["mission_id"])

    _banner("B. Known conflicting registration")
    final = orchestrator.run_mission(_goal("Tech Talk: Cloud Native Systems"), user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT)
    _print_plan(final)
    _print_verdicts(final, "After run")
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, _action(final).task_id)
    print(f"Approval created: {approval is not None}; registration rows for event 6: {_registrations(session_factory, 6)}")
    print(f"Final response: {final['final_result']}")
    _print_audit(session_factory, final["mission_id"])

    _banner("D. Deterministic failure (full event)")
    final = orchestrator.run_mission(_goal("Startup Pitch Night"), user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT)
    _print_verdicts(final, "After run")
    print(f"replan_count={final['replan_count']} duplicate_failure_stop={final.get('duplicate_failure_stop')}")
    print(f"Final response: {final['final_result']}")
    _print_audit(session_factory, final["mission_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
