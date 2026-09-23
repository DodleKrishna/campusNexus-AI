"""Task dispatch: builds AgentMessages and invokes the registry's agents.

Ready tasks are dispatched concurrently via a ``ThreadPoolExecutor``, each
against its **own** fresh ``Session`` built from ``session_factory`` --
SQLAlchemy's ``Session`` is not thread-safe, so sharing one across
concurrently-running agent calls would be a real correctness bug, not a
style choice. This is what makes "independent tasks eligible for parallel
execution" (CLAUDE.md DAG Execution) genuinely true rather than nominal.

Phase 6 addition -- dependency-aware information sharing: a task's message
is built from ``base_facts`` (student_id/as_of) *plus* the merged
``AgentResult.facts`` of every one of its dependencies (e.g. the Career
Agent's ``skill_gaps`` reaching a dependent Events Agent task). This is safe
because a task only ever becomes dispatchable once every dependency has
reached ``COMPLETED`` (app/graph/scheduler.py) -- so by the time this runs,
``agent_results`` is guaranteed to already hold a real entry for each
dependency id; nothing here is ever guessed or fabricated.

Phase 7 addition -- ``MissionTask.constraints`` (a Phase 1 field, unused
until now) is now forwarded into ``AgentMessage.constraints`` too. The
Action Agent uses it as its structured tool-target input (tool name +
resource parameters); every other agent still ignores it, so this is purely
additive.
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy.orm import Session, sessionmaker

from app.graph.registry import AgentRegistry, UnsupportedAgentError
from app.graph.results import AgentOutcome
from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.common import JsonValue
from app.schemas.enums import AgentName
from app.schemas.mission import MissionPlan, MissionTask


@dataclass
class DispatchOutcome:
    """Either a successful ``AgentOutcome`` or a captured dispatch-time error.

    A dispatch-time error (unsupported agent, an unhandled exception inside
    the agent call) never propagates out of ``dispatch_ready_tasks`` -- it's
    captured here so one task's failure can't crash the whole mission run or
    silently vanish; the caller turns it into that task's ``FAILED`` status.
    """

    task_id: str
    outcome: Optional[AgentOutcome]
    error: Optional[str] = None


def _build_message(
    mission_id: str,
    task: MissionTask,
    base_facts: Dict[str, JsonValue],
    agent_results: Dict[str, AgentResult],
) -> AgentMessage:
    facts = dict(base_facts)
    for dependency_id in task.dependencies:
        upstream = agent_results.get(dependency_id)
        if upstream is not None:
            facts.update(upstream.facts)
    facts["query"] = task.objective
    return AgentMessage(
        message_id=f"msg-{uuid.uuid4().hex[:12]}",
        mission_id=mission_id,
        task_id=task.task_id,
        source=AgentName.MISSION_ORCHESTRATOR,
        target=task.agent,
        objective=task.objective,
        facts=facts,
        constraints=dict(task.constraints),
    )


def _run_one(
    task: MissionTask,
    mission_id: str,
    base_facts: Dict[str, JsonValue],
    agent_results: Dict[str, AgentResult],
    registry: AgentRegistry,
    session_factory: sessionmaker[Session],
) -> DispatchOutcome:
    session = session_factory()
    try:
        agent = registry.build(task.agent, session)
        message = _build_message(mission_id, task, base_facts, agent_results)
        outcome = agent.handle(message)
        return DispatchOutcome(task_id=task.task_id, outcome=outcome)
    except UnsupportedAgentError as exc:
        return DispatchOutcome(task_id=task.task_id, outcome=None, error=str(exc))
    except Exception as exc:  # noqa: BLE001 -- any agent-internal failure becomes a task failure, not a crash
        return DispatchOutcome(task_id=task.task_id, outcome=None, error=f"{type(exc).__name__}: {exc}")
    finally:
        session.close()


def dispatch_ready_tasks(
    plan: MissionPlan,
    ready_task_ids: List[str],
    *,
    base_facts: Dict[str, JsonValue],
    agent_results: Dict[str, AgentResult],
    registry: AgentRegistry,
    session_factory: sessionmaker[Session],
    max_workers: int = 4,
) -> Dict[str, DispatchOutcome]:
    """Dispatch every ready task concurrently, each against its own DB session.

    ``agent_results`` is the mission's already-collected results so far --
    used to forward each dispatched task's dependencies' facts (see module
    docstring); tasks with no dependencies are unaffected.
    """
    tasks_by_id = {task.task_id: task for task in plan.tasks}
    ready_tasks = [tasks_by_id[task_id] for task_id in ready_task_ids]
    if not ready_tasks:
        return {}

    results: Dict[str, DispatchOutcome] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(ready_tasks))) as executor:
        futures = {
            executor.submit(
                _run_one, task, plan.mission_id, base_facts, agent_results, registry, session_factory
            ): task.task_id
            for task in ready_tasks
        }
        for future in futures:
            results[futures[future]] = future.result()
    return results
