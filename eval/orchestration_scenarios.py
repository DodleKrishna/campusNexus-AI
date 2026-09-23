"""Deterministic Mission Orchestrator evaluation (no live LLM, no network).

Mirrors eval/run_rag_eval.py and eval/run_academic_eval.py's PASS/FAIL
structure, but is plain Python rather than a JSON scenario file: DAG shapes
(dependencies, parallelism, failure/retry) need real Python objects
(MissionPlan/MissionTask graphs, deterministic test-double agents) to
construct, which JSON can't express concisely. Uses the same deterministic
test-double agents as tests/test_orchestrator*.py (tests/graph_doubles.py)
-- per the Phase 5 spec, these are registered only for this evaluation and
under tests/, never as production specialist agents.

Runs each scenario against its own temp SQLite database (not the dev DB --
these are synthetic DAGs, not real academic missions) via the real
MissionOrchestrator/Context Service. Exits non-zero if any scenario fails.

Usage:
    python eval/orchestration_scenarios.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.db.base import utc_now  # noqa: E402
from app.db.session import create_db_engine, create_session_factory, init_db  # noqa: E402
from app.graph.orchestrator import MissionOrchestrator  # noqa: E402
from app.graph.registry import AgentRegistry  # noqa: E402
from app.schemas.enums import AgentName, AgentResultStatus, MissionStatus, TaskStatus, UserRole  # noqa: E402
from app.schemas.mission import MissionPlan, MissionTask  # noqa: E402
from app.services.context import ContextService  # noqa: E402
from graph_doubles import (  # noqa: E402
    AlwaysFailAgent,
    FixedPlanLLMProvider,
    MismatchedVerificationAgent,
    NeedsReviewAgent,
    SuccessAgent,
)

MISSION_USER = "STU-DEMO-001"


def _task(mission_id: str, index: int, agent: AgentName = AgentName.ACADEMIC_AGENT, dependencies=None) -> MissionTask:
    return MissionTask(
        task_id=f"{mission_id}-task-{index}",
        mission_id=mission_id,
        agent=agent,
        objective=f"synthetic task {index}",
        dependencies=dependencies or [],
    )


def _registry(agents: Dict[AgentName, Callable]) -> AgentRegistry:
    registry = AgentRegistry()
    for name, factory in agents.items():
        registry.register(name, factory)
    return registry


def _new_orchestrator(session_factory, registry, plan_factory, max_replans=2) -> MissionOrchestrator:
    llm_provider = FixedPlanLLMProvider(plan_factory=plan_factory)
    return MissionOrchestrator(
        session_factory=session_factory, registry=registry, llm_provider=llm_provider, max_replans=max_replans
    )


# ---------------------------------------------------------------------------
# Scenarios: each takes a fresh session_factory, returns (passed, detail).
# ---------------------------------------------------------------------------


def scenario_valid_single_agent_mission(session_factory) -> Dict[str, Any]:
    orchestrator = _new_orchestrator(
        session_factory,
        _registry({AgentName.ACADEMIC_AGENT: lambda s: SuccessAgent(s)}),
        lambda mid, goal: MissionPlan(mission_id=mid, goal=goal, tasks=[_task(mid, 1)]),
    )
    final = orchestrator.run_mission("single task", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    passed = final["mission_status"] == MissionStatus.COMPLETED
    return {"passed": passed, "mission_status": final["mission_status"].value}


def scenario_valid_multi_task_mission(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        return MissionPlan(mission_id=mid, goal=goal, tasks=[_task(mid, 1), _task(mid, 2), _task(mid, 3)])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: SuccessAgent(s)}), plan_factory)
    final = orchestrator.run_mission("three tasks", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    passed = final["mission_status"] == MissionStatus.COMPLETED and len(final["task_status"]) == 3
    return {"passed": passed, "mission_status": final["mission_status"].value, "task_count": len(final["task_status"])}


def scenario_correct_task_dependencies(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        a = _task(mid, 1)
        b = _task(mid, 2, dependencies=[a.task_id])
        return MissionPlan(mission_id=mid, goal=goal, tasks=[a, b])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: SuccessAgent(s)}), plan_factory)
    final = orchestrator.run_mission("dependency chain", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    passed = final["mission_status"] == MissionStatus.COMPLETED and all(
        s == TaskStatus.COMPLETED for s in final["task_status"].values()
    )
    return {"passed": passed, "task_status": {k: v.value for k, v in final["task_status"].items()}}


def scenario_parallel_independent_tasks(session_factory) -> Dict[str, Any]:
    lock = threading.Lock()
    tracker = {"current": 0, "peak": 0}

    class TrackedAgent(SuccessAgent):
        def handle(self, message):
            with lock:
                tracker["current"] += 1
                tracker["peak"] = max(tracker["peak"], tracker["current"])
            time.sleep(0.05)
            with lock:
                tracker["current"] -= 1
            return super().handle(message)

    def plan_factory(mid, goal):
        return MissionPlan(mission_id=mid, goal=goal, tasks=[_task(mid, i) for i in range(1, 4)])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: TrackedAgent(s)}), plan_factory)
    final = orchestrator.run_mission("parallel tasks", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    passed = final["mission_status"] == MissionStatus.COMPLETED and tracker["peak"] > 1
    return {"passed": passed, "peak_concurrency": tracker["peak"]}


def scenario_invalid_agent_assignment(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        return MissionPlan(mission_id=mid, goal=goal, tasks=[_task(mid, 1, agent=AgentName.CAREER_AGENT)])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: SuccessAgent(s)}), plan_factory)
    final = orchestrator.run_mission("bad agent", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    passed = final["mission_status"] == MissionStatus.FAILED and bool(final["validation_errors"])
    return {"passed": passed, "validation_errors": final["validation_errors"]}


def scenario_missing_dependency(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        return MissionPlan(mission_id=mid, goal=goal, tasks=[_task(mid, 1, dependencies=["nope"])])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: SuccessAgent(s)}), plan_factory)
    final = orchestrator.run_mission("missing dep", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    passed = final["mission_status"] == MissionStatus.FAILED
    return {"passed": passed, "validation_errors": final["validation_errors"]}


def scenario_circular_dependency(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        a = MissionTask(task_id=f"{mid}-a", mission_id=mid, agent=AgentName.ACADEMIC_AGENT, objective="task a", dependencies=[f"{mid}-b"])
        b = MissionTask(task_id=f"{mid}-b", mission_id=mid, agent=AgentName.ACADEMIC_AGENT, objective="task b", dependencies=[f"{mid}-a"])
        return MissionPlan(mission_id=mid, goal=goal, tasks=[a, b])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: SuccessAgent(s)}), plan_factory)
    final = orchestrator.run_mission("cycle", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    passed = final["mission_status"] == MissionStatus.FAILED
    return {"passed": passed, "validation_errors": final["validation_errors"]}


def scenario_agent_failure_blocks_dependents(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        a = _task(mid, 1)
        b = _task(mid, 2, dependencies=[a.task_id])
        return MissionPlan(mission_id=mid, goal=goal, tasks=[a, b])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: AlwaysFailAgent(s)}), plan_factory, max_replans=0)
    final = orchestrator.run_mission("upstream fails", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    statuses = {k: v.value for k, v in final["task_status"].items()}
    passed = final["mission_status"] == MissionStatus.FAILED and set(statuses.values()) == {"failed", "skipped"}
    return {"passed": passed, "task_status": statuses}


def scenario_verification_failure_despite_successful_call(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        return MissionPlan(mission_id=mid, goal=goal, tasks=[_task(mid, 1)])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: MismatchedVerificationAgent(s)}), plan_factory, max_replans=0)
    final = orchestrator.run_mission("looks fine but isn't", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    task_id = f"{final['mission_id']}-task-1"
    passed = (
        final["mission_status"] == MissionStatus.FAILED
        and final["agent_results"][task_id].status == AgentResultStatus.SUCCESS
        and final["task_status"][task_id] == TaskStatus.FAILED
    )
    return {"passed": passed, "mission_status": final["mission_status"].value}


def scenario_needs_review_pauses_without_auto_approving(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        return MissionPlan(mission_id=mid, goal=goal, tasks=[_task(mid, 1)])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: NeedsReviewAgent(s)}), plan_factory)
    final = orchestrator.run_mission("ambiguous", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    passed = final["mission_status"] == MissionStatus.NEEDS_APPROVAL
    return {"passed": passed, "mission_status": final["mission_status"].value}


def scenario_retry_limit_is_bounded(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        return MissionPlan(mission_id=mid, goal=goal, tasks=[_task(mid, 1)])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: AlwaysFailAgent(s)}), plan_factory, max_replans=2)
    final = orchestrator.run_mission("always fails", user_id=MISSION_USER, user_role=UserRole.STUDENT)
    passed = final["mission_status"] == MissionStatus.FAILED and final["replan_count"] == 2
    return {"passed": passed, "replan_count": final["replan_count"]}


def scenario_interrupted_mission_and_resumption(session_factory) -> Dict[str, Any]:
    mission_id = "mission-eval-resume"
    task_a, task_b = _task(mission_id, 1), _task(mission_id, 2, dependencies=[f"{mission_id}-task-1"])
    plan = MissionPlan(mission_id=mission_id, goal="two step", tasks=[task_a, task_b])

    with session_factory() as session:
        context = ContextService(session)
        context.create_mission(mission_id, MISSION_USER, UserRole.STUDENT, plan.goal)
        context.append_audit_event(
            event_id="evt-setup", mission_id=mission_id, event_type="plan_generated",
            actor="eval-setup", message="plan", metadata={"plan": plan.model_dump(mode="json")},
        )
        context.create_mission_step(step_id=task_a.task_id, mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT, objective=task_a.objective, sequence=0)
        context.create_mission_step(step_id=task_b.task_id, mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT, objective=task_b.objective, sequence=1)
        context.record_agent_result(mission_id=mission_id, step_id=task_a.task_id, agent=AgentName.ACADEMIC_AGENT, status=AgentResultStatus.SUCCESS, facts={}, errors=[])
        context.update_mission_step(task_a.task_id, status=TaskStatus.COMPLETED, completed_at=utc_now())
        context.update_mission_status(mission_id, MissionStatus.IN_PROGRESS)

    class GuardAgent(SuccessAgent):
        def handle(self, message):
            if message.task_id == task_a.task_id:
                raise AssertionError("completed task re-dispatched on resume")
            return super().handle(message)

    orchestrator = _new_orchestrator(
        session_factory,
        _registry({AgentName.ACADEMIC_AGENT: lambda s: GuardAgent(s)}),
        plan_factory=lambda mid, goal: (_ for _ in ()).throw(AssertionError("plan_mission called on resume")),
    )
    final = orchestrator.resume_mission(mission_id)
    passed = final["mission_status"] == MissionStatus.COMPLETED and all(
        s == TaskStatus.COMPLETED for s in final["task_status"].values()
    )
    return {"passed": passed, "mission_status": final["mission_status"].value}


def scenario_persisted_audit_history(session_factory) -> Dict[str, Any]:
    def plan_factory(mid, goal):
        return MissionPlan(mission_id=mid, goal=goal, tasks=[_task(mid, 1)])

    orchestrator = _new_orchestrator(session_factory, _registry({AgentName.ACADEMIC_AGENT: lambda s: SuccessAgent(s)}), plan_factory)
    final = orchestrator.run_mission("audit trail", user_id=MISSION_USER, user_role=UserRole.STUDENT)

    with session_factory() as session:
        context = ContextService(session)
        events = [e.event_type for e in context.list_audit_events(final["mission_id"])]
        runs = context.list_agent_runs(final["mission_id"])

    expected = {"mission_created", "plan_generated", "task_verified", "mission_finalized"}
    passed = expected.issubset(set(events)) and len(runs) == 1
    return {"passed": passed, "audit_event_types": events, "agent_run_count": len(runs)}


SCENARIOS = [
    ("valid-single-agent-mission", scenario_valid_single_agent_mission),
    ("valid-multi-task-mission", scenario_valid_multi_task_mission),
    ("correct-task-dependencies", scenario_correct_task_dependencies),
    ("parallel-independent-tasks", scenario_parallel_independent_tasks),
    ("invalid-agent-assignment", scenario_invalid_agent_assignment),
    ("missing-dependency", scenario_missing_dependency),
    ("circular-dependency", scenario_circular_dependency),
    ("agent-failure-blocks-dependents", scenario_agent_failure_blocks_dependents),
    ("verification-failure-despite-successful-call", scenario_verification_failure_despite_successful_call),
    ("needs-review-pauses-without-auto-approving", scenario_needs_review_pauses_without_auto_approving),
    ("retry-limit-is-bounded", scenario_retry_limit_is_bounded),
    ("interrupted-mission-and-resumption", scenario_interrupted_mission_and_resumption),
    ("persisted-audit-history", scenario_persisted_audit_history),
]


def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="campusnexus_orchestration_eval_"))
    failures = 0
    try:
        for name, scenario_fn in SCENARIOS:
            db_path = tmp_dir / f"{name}.db"
            engine = create_db_engine(db_path=str(db_path))
            init_db(engine)
            session_factory = create_session_factory(engine)

            try:
                result = scenario_fn(session_factory)
            except Exception as exc:  # noqa: BLE001 -- a scenario raising is itself a FAIL, not a crash of the runner
                result = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                engine.dispose()

            status = "PASS" if result.get("passed") else "FAIL"
            if not result.get("passed"):
                failures += 1
            print(f"{status} {name}")
            for key, value in result.items():
                if key != "passed":
                    print(f"     {key}: {value}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    total = len(SCENARIOS)
    print(f"\n{total - failures}/{total} scenarios passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
