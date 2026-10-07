"""AgentRuntime: one bounded Observe -> Decide -> Act -> Observe -> Verify transition per ``run_step``.

``run_step`` reloads the mission in the caller's tenant session, checks scope and
ownership, refuses terminal missions, gathers observations (consumed events, a
wake-up, the previous result), asks the AgentBrain for ONE typed decision,
validates it against the agent's tool/delegation allowlists and the tool's input
schema, executes at most one bounded action, persists one ``AgentStep`` and the
mission's new state, audits it, and commits. There is no loop here:
``run_until_blocked`` repeats ``run_step`` under a hard cap.

Invalid decisions (bad shape, unlisted tool or agent, bad tool input) are never
executed: the step is recorded REJECTED and the mission FAILS explicitly.
Nothing a brain says is persisted except redacted, structured summaries of the
observable decision and its result -- never reasoning.

Audit events (``operation_audit_events``, subject ``agent_mission``): MISSION_CREATED,
MISSION_STARTED, AGENT_STEP_EXECUTED, TOOL_EXECUTED, AGENT_DELEGATED,
MISSION_WAITING, MISSION_COMPLETED, MISSION_FAILED, MISSION_CANCELLED.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agentos.brain import AgentBrain, BrainUnavailableError
from app.agentos.events import SUBJECT_MISSION, EventService
from app.agentos.registry import AgentRegistry, AgentSpec, ToolContext, ToolExecutionError, ToolRegistry, ToolResult
from app.agentos.safety import bounded, redact
from app.agentos.schemas import (
    AgentContext, AgentDecision, AgentObservation, AgentResult, CreateAgentMission, DecisionKind, DomainEventType,
)
from app.db.base import utc_now
from app.db.models.agent_kernel import (
    TERMINAL_MISSION_STATUSES, WAITING_MISSION_STATUSES, AgentMission, AgentMissionStatus, AgentStep, AgentStepStatus,
)
from app.db.repositories import operations_audit
from app.db.tenant_session import session_organization
from app.schemas.enums import UserRole

HARD_TRANSITION_CAP = 25
MAX_DELEGATION_DEPTH = 3
RECENT_OBSERVATIONS = 5
SUBJECT = "agent_mission"


@dataclass(frozen=True)
class MissionActor:
    """The server-resolved caller (from the verified token / tenant context). Never built from client input."""

    organization_id: int
    account_id: int
    role: UserRole


class AgentOSError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class _Rejected(Exception):
    def __init__(self, code: str, summary: Dict[str, Any]) -> None:
        super().__init__(code)
        self.code, self.summary = code, summary


@dataclass
class _Outcome:
    step_status: AgentStepStatus
    output: Dict[str, Any]
    tool_name: Optional[str] = None
    delegated_agent: Optional[str] = None
    latency_ms: Optional[int] = None
    error_code: Optional[str] = None


class AgentRuntime:
    def __init__(self, agents: AgentRegistry, tools: ToolRegistry, brain: AgentBrain, *,
                 events: Optional[EventService] = None, clock: Callable[[], datetime] = utc_now) -> None:
        self.agents, self.tools, self.brain = agents, tools, brain
        self.events = events or EventService()
        self.clock = clock

    # --- Access ------------------------------------------------------------------------------------------------

    @staticmethod
    def _check_session(session: Session, actor: MissionActor) -> None:
        # Fail closed: only a tenant session bound to the caller's own organization is accepted.
        if session_organization(session) != actor.organization_id:
            raise AgentOSError("TENANT_MISMATCH", "This session is not bound to your organization.", 403)

    def _load(self, session: Session, actor: MissionActor, mission_id: int, *, owner_only: bool) -> AgentMission:
        self._check_session(session, actor)
        mission = session.get(AgentMission, mission_id)  # another organization's mission is simply not found
        allowed = mission is not None and mission.organization_id == actor.organization_id and (
            mission.owner_account_id == actor.account_id or (not owner_only and actor.role == UserRole.ADMIN))
        if not allowed:
            raise AgentOSError("MISSION_NOT_FOUND", "Mission not found.", 404)
        return mission

    def _agent(self, agent_key: str, role: UserRole) -> AgentSpec:
        spec = self.agents.get(agent_key)
        if spec is None:
            raise AgentOSError("AGENT_NOT_REGISTERED", f"Agent '{agent_key}' is not registered.", 400)
        if role not in spec.supported_roles:
            raise AgentOSError("AGENT_ROLE_NOT_ALLOWED", f"Agent '{agent_key}' is not available for your role.", 403)
        return spec

    # --- Public operations ---------------------------------------------------------------------------------------

    def create_mission(self, session: Session, actor: MissionActor, body: CreateAgentMission) -> AgentMission:
        self._check_session(session, actor)
        self._agent(body.agent_key, actor.role)
        inputs = bounded(body.context)
        if inputs.get("_truncated"):
            raise AgentOSError("CONTEXT_TOO_LARGE", "Mission context is too large.", 400)
        mission = self._new_mission(session, actor, agent_key=body.agent_key, goal=body.goal,
                                    success_criteria=body.success_criteria, priority=body.priority,
                                    max_steps=body.max_steps, context={"inputs": inputs, "observations": []})
        session.commit()
        return mission

    def get_mission(self, session: Session, actor: MissionActor, mission_id: int) -> AgentMission:
        return self._load(session, actor, mission_id, owner_only=False)

    def list_steps(self, session: Session, actor: MissionActor, mission_id: int) -> List[AgentStep]:
        mission = self._load(session, actor, mission_id, owner_only=False)
        return list(session.execute(
            select(AgentStep).where(AgentStep.mission_id == mission.id).order_by(AgentStep.step_number)).scalars())

    def cancel(self, session: Session, actor: MissionActor, mission_id: int) -> AgentMission:
        mission = self._load(session, actor, mission_id, owner_only=False)
        if mission.status in TERMINAL_MISSION_STATUSES:
            raise AgentOSError("MISSION_TERMINAL", f"Mission is already {mission.status.value}.", 409)
        now = self.clock()
        self._finish(session, actor, mission, AgentMissionStatus.CANCELLED, now, "MISSION_CANCELLED",
                     f"Mission {mission.id} cancelled.", {})
        session.commit()
        return mission

    def run_until_blocked(self, session: Session, actor: MissionActor, mission_id: int,
                          max_transitions: int = 5) -> List[AgentResult]:
        """Repeat ``run_step`` until the mission stops RUNNING, nothing ran, or ``max_transitions`` (hard-capped)."""
        if not 1 <= max_transitions <= HARD_TRANSITION_CAP:
            raise AgentOSError("INVALID_TRANSITION_LIMIT", f"max_transitions must be 1-{HARD_TRANSITION_CAP}.", 400)
        results: List[AgentResult] = []
        for _ in range(max_transitions):
            result = self.run_step(session, actor, mission_id)
            results.append(result)
            if not result.transitioned or result.status != AgentMissionStatus.RUNNING:
                break
        return results

    def run_step(self, session: Session, actor: MissionActor, mission_id: int) -> AgentResult:
        mission = self._load(session, actor, mission_id, owner_only=True)
        if mission.status in TERMINAL_MISSION_STATUSES:
            raise AgentOSError("MISSION_TERMINAL", f"Mission is already {mission.status.value}.", 409)
        spec = self._agent(mission.agent_key, actor.role)
        now = self.clock()

        if mission.step_count >= mission.max_steps:
            self._finish(session, actor, mission, AgentMissionStatus.FAILED, now, "MISSION_FAILED",
                         f"Mission {mission.id} reached its step limit.", {"code": "MAX_STEPS_EXCEEDED"})
            session.commit()
            return AgentResult(mission_id=mission.id, transitioned=True, status=mission.status, error_code="MAX_STEPS_EXCEEDED")

        # Observe
        observations: List[AgentObservation] = []
        if mission.status in WAITING_MISSION_STATUSES:
            event = self.events.next_for_mission(session, mission)
            if event is not None:
                self.events.consume(event, now)
                observations.append(AgentObservation(kind="event", source=event.event_type, at=now,
                                                     data={"event_id": event.id, "payload": event.payload}))
            elif mission.next_wake_at is not None and now >= mission.next_wake_at:
                observations.append(AgentObservation(kind="wake", source="timer", at=now))
            else:
                return AgentResult(mission_id=mission.id, transitioned=False, status=mission.status,
                                   detail=f"waiting for {mission.waiting_for}")
            mission.status, mission.waiting_for, mission.next_wake_at = AgentMissionStatus.RUNNING, None, None
        elif mission.status == AgentMissionStatus.PENDING:
            mission.status = AgentMissionStatus.RUNNING
            self._audit(session, actor, mission, "MISSION_STARTED", f"Mission {mission.id} started.", {}, now)
            observations.append(AgentObservation(kind="mission_started", source="runtime", at=now))
        self._remember(mission, observations)

        # Decide
        try:
            raw = self.brain.decide(self._context(mission, spec, actor))
        except BrainUnavailableError:
            session.rollback()  # nothing happened: the mission, events and observations are unchanged
            mission = self._load(session, actor, mission_id, owner_only=True)
            return AgentResult(mission_id=mission.id, transitioned=False, status=mission.status,
                               error_code="BRAIN_UNAVAILABLE")
        except Exception as exc:  # noqa: BLE001 -- a broken brain fails the mission visibly, never silently
            return self._reject(session, actor, mission, _Rejected("BRAIN_ERROR", {"error": type(exc).__name__}), now)

        # Validate, then act (at most one bounded action)
        try:
            decision = raw if isinstance(raw, AgentDecision) else AgentDecision.model_validate(raw)
            outcome = self._act(session, actor, mission, spec, decision, now)
        except _Rejected as rejected:
            return self._reject(session, actor, mission, rejected, now)
        except ValidationError:
            kind = raw.get("kind") if isinstance(raw, dict) else None
            summary = {"kind": kind if isinstance(kind, str) and kind in {k.value for k in DecisionKind} else "invalid"}
            return self._reject(session, actor, mission, _Rejected("INVALID_DECISION", summary), now)
        except _ToolCrashed as crashed:
            return self._record_crash(session, actor, mission_id, decision, crashed, now)

        step = self._record_step(session, actor, mission, decision, outcome, now)
        return self._commit(session, mission, step, decision.kind, outcome.error_code)

    # --- Acting ----------------------------------------------------------------------------------------------------

    def _act(self, session: Session, actor: MissionActor, mission: AgentMission, spec: AgentSpec,
             decision: AgentDecision, now: datetime) -> _Outcome:
        kind = decision.kind
        if kind == DecisionKind.TOOL:
            return self._run_tool(session, actor, mission, spec, decision, now)
        if kind == DecisionKind.DELEGATE:
            return self._delegate(session, actor, mission, spec, decision, now)
        if kind == DecisionKind.WAIT:
            event = decision.wait_for
            mission.status = (AgentMissionStatus.WAITING_CONNECTIVITY if event == DomainEventType.NETWORK_RESTORED
                              else AgentMissionStatus.WAITING_EVENT)
            mission.waiting_for = event.value
            mission.next_wake_at = now + timedelta(seconds=decision.wake_after_seconds) if decision.wake_after_seconds else None
            self._audit(session, actor, mission, "MISSION_WAITING", f"Mission {mission.id} is waiting for {event.value}.",
                        {"waiting_for": event.value, "status": mission.status.value}, now)
            return _Outcome(AgentStepStatus.EXECUTED, {"waiting_for": event.value, "status": mission.status.value})
        if kind == DecisionKind.ASK_HUMAN:
            mission.status, mission.waiting_for = AgentMissionStatus.WAITING_HUMAN, DomainEventType.HUMAN_RESPONDED.value
            mission.context = {**mission.context, "pending_question": redact(decision.question)}
            self._audit(session, actor, mission, "MISSION_WAITING", f"Mission {mission.id} is waiting for a person.",
                        {"waiting_for": mission.waiting_for, "status": mission.status.value}, now)
            return _Outcome(AgentStepStatus.EXECUTED, {"waiting_for": mission.waiting_for})
        if kind == DecisionKind.REPLAN:
            version = int((mission.current_plan or {}).get("version", 0)) + 1
            mission.current_plan = {"version": version, "steps": redact(decision.plan)}
            self._remember(mission, [AgentObservation(kind="plan_updated", source="runtime", at=now, data={"version": version})])
            return _Outcome(AgentStepStatus.EXECUTED, {"plan_version": version})
        if kind == DecisionKind.COMPLETE:
            mission.context = {**mission.context, "outcome": redact(decision.outcome)}
            self._finish(session, actor, mission, AgentMissionStatus.COMPLETED, now, "MISSION_COMPLETED",
                         f"Mission {mission.id} completed.", {})
            return _Outcome(AgentStepStatus.EXECUTED, {"status": "completed"})
        # FAIL
        self._finish(session, actor, mission, AgentMissionStatus.FAILED, now, "MISSION_FAILED",
                     f"Mission {mission.id} failed.", {"code": "AGENT_FAILED", "reason": redact(decision.reason)})
        return _Outcome(AgentStepStatus.EXECUTED, {"status": "failed"})

    def _run_tool(self, session: Session, actor: MissionActor, mission: AgentMission, spec: AgentSpec,
                  decision: AgentDecision, now: datetime) -> _Outcome:
        name = decision.tool_name
        tool = self.tools.get(name) if name in spec.allowed_tools else None
        if tool is None or actor.role not in tool.required_roles:
            raise _Rejected("UNAUTHORIZED_TOOL", {"tool_name": name})
        try:
            args = tool.input_model.model_validate(decision.tool_input)
        except ValidationError as exc:
            fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})[:10]
            raise _Rejected("INVALID_TOOL_INPUT", {"tool_name": name, "invalid_fields": fields}) from None
        context = ToolContext(session=session, organization_id=actor.organization_id, account_id=actor.account_id,
                              role=actor.role, mission_id=mission.id, now=now)
        pending_before = set(session.new) | set(session.deleted)
        started = time.perf_counter()
        try:
            result = tool.execute(context, args)
            if not isinstance(result, ToolResult):
                raise TypeError("tool returned an unstructured result")
            if (set(session.new) | set(session.deleted)) != pending_before:
                raise PermissionError("read-only tool changed data")
        except ToolExecutionError as exc:
            result = ToolResult(ok=False, error_code=exc.code[:64])
        except Exception as exc:  # noqa: BLE001
            raise _ToolCrashed(name, type(exc).__name__) from None
        latency = int((time.perf_counter() - started) * 1000)
        data = bounded(result.data)
        self._remember(mission, [AgentObservation(kind="tool_result" if result.ok else "tool_error", source=name, at=now,
                                                  data={"ok": result.ok, "data": data, "error_code": result.error_code})])
        self._audit(session, actor, mission, "TOOL_EXECUTED", f"Tool {name} ran for mission {mission.id}.",
                    {"tool": name, "ok": result.ok, "error_code": result.error_code, "latency_ms": latency}, now)
        return _Outcome(AgentStepStatus.EXECUTED if result.ok else AgentStepStatus.FAILED,
                        {"ok": result.ok, "data": data, "error_code": result.error_code},
                        tool_name=name, latency_ms=latency, error_code=result.error_code)

    def _delegate(self, session: Session, actor: MissionActor, mission: AgentMission, spec: AgentSpec,
                  decision: AgentDecision, now: datetime) -> _Outcome:
        target = decision.delegate_agent
        child_spec = self.agents.get(target) if target in spec.allowed_delegate_agents else None
        if child_spec is None or actor.role not in child_spec.supported_roles:
            raise _Rejected("UNAUTHORIZED_DELEGATION", {"delegate_agent": target})
        depth = int(mission.context.get("delegation_depth", 0)) + 1
        if depth > MAX_DELEGATION_DEPTH:
            raise _Rejected("DELEGATION_TOO_DEEP", {"delegate_agent": target})
        remaining = max(mission.max_steps - mission.step_count - 1, 1)
        child = self._new_mission(session, actor, agent_key=target, goal=decision.delegate_goal, success_criteria=[],
                                  priority=mission.priority, max_steps=min(remaining, mission.max_steps),
                                  context={"inputs": {}, "observations": [], "parent_mission_id": mission.id,
                                           "delegation_depth": depth})
        mission.status, mission.waiting_for = AgentMissionStatus.WAITING_EVENT, DomainEventType.DELEGATION_FINISHED.value
        self._remember(mission, [AgentObservation(kind="delegated", source=target, at=now, data={"child_mission_id": child.id})])
        self._audit(session, actor, mission, "AGENT_DELEGATED", f"Mission {mission.id} delegated to {target}.",
                    {"delegate_agent": target, "child_mission_id": child.id}, now)
        self._audit(session, actor, mission, "MISSION_WAITING", f"Mission {mission.id} is waiting for {target}.",
                    {"waiting_for": mission.waiting_for, "status": mission.status.value}, now)
        return _Outcome(AgentStepStatus.EXECUTED, {"child_mission_id": child.id}, delegated_agent=target)

    # --- Persistence helpers ---------------------------------------------------------------------------------------

    def _new_mission(self, session: Session, actor: MissionActor, *, agent_key: str, goal: str,
                     success_criteria: List[str], priority: str, max_steps: int, context: Dict[str, Any]) -> AgentMission:
        mission = AgentMission(
            organization_id=actor.organization_id, owner_account_id=actor.account_id, created_by_account_id=actor.account_id,
            agent_key=agent_key, goal=redact(goal), success_criteria=[redact(c)[:300] for c in success_criteria],
            status=AgentMissionStatus.PENDING, priority=priority, context=context, max_steps=max_steps, step_count=0,
        )
        session.add(mission)
        session.flush()
        self._audit(session, actor, mission, "MISSION_CREATED", f"Mission {mission.id} created for {agent_key}.",
                    {"agent_key": agent_key, "max_steps": max_steps, "parent_mission_id": context.get("parent_mission_id")},
                    self.clock())
        return mission

    def _context(self, mission: AgentMission, spec: AgentSpec, actor: MissionActor) -> AgentContext:
        state = {k: v for k, v in (mission.context or {}).items() if k != "observations"}
        return AgentContext(
            mission_id=mission.id, agent_key=mission.agent_key, goal=mission.goal,
            success_criteria=list(mission.success_criteria or []), status=mission.status, step_count=mission.step_count,
            max_steps=mission.max_steps, plan=mission.current_plan, state=redact(state),
            observations=[AgentObservation.model_validate(o) for o in mission.context.get("observations", [])],
            allowed_tools=self.tools.describe(spec.allowed_tools, actor.role),
            allowed_delegate_agents=sorted(a for a in spec.allowed_delegate_agents if self.agents.get(a)),
        )

    @staticmethod
    def _remember(mission: AgentMission, observations: List[AgentObservation]) -> None:
        if not observations:
            return
        recent = list((mission.context or {}).get("observations", []))
        for observation in observations:
            stored = bounded(observation.model_dump(mode="json"))
            if "kind" not in stored:  # too large: keep a valid, empty observation rather than the payload
                stored = {**observation.model_dump(mode="json", exclude={"data"}), "data": {"_truncated": True}}
            recent.append(stored)
        mission.context = {**(mission.context or {}), "observations": recent[-RECENT_OBSERVATIONS:]}

    def _audit(self, session: Session, actor: MissionActor, mission: AgentMission, event_type: str, message: str,
               metadata: Dict[str, Any], now: datetime) -> None:
        operations_audit.record(
            session, event_type=event_type, actor_account_id=actor.account_id, actor_role=actor.role.value,
            subject_type=SUBJECT, subject_id=str(mission.id), message=message, at=now,
            metadata=bounded({"agent_key": mission.agent_key, **metadata}),
        )

    def _finish(self, session: Session, actor: MissionActor, mission: AgentMission, status: AgentMissionStatus,
                now: datetime, event_type: str, message: str, metadata: Dict[str, Any]) -> None:
        mission.status, mission.completed_at = status, now
        mission.waiting_for, mission.next_wake_at = None, None
        if metadata.get("code"):
            mission.context = {**(mission.context or {}), "failure": bounded(metadata)}
        self._audit(session, actor, mission, event_type, message, {"status": status.value, **metadata}, now)
        parent_id = (mission.context or {}).get("parent_mission_id")
        if parent_id is not None:
            self.events.publish(session, event_type=DomainEventType.DELEGATION_FINISHED, actor_account_id=actor.account_id,
                                subject_type=SUBJECT_MISSION, subject_id=parent_id, now=now,
                                payload={"child_mission_id": mission.id, "status": status.value,
                                         "outcome": (mission.context or {}).get("outcome")})

    def _record_step(self, session: Session, actor: MissionActor, mission: AgentMission, decision: Optional[AgentDecision],
                     outcome: _Outcome, now: datetime, *, rejected_summary: Optional[Dict[str, Any]] = None) -> AgentStep:
        mission.step_count += 1
        input_summary = rejected_summary if decision is None else _decision_summary(decision)
        step = AgentStep(
            organization_id=actor.organization_id, mission_id=mission.id, step_number=mission.step_count,
            agent_key=mission.agent_key, action_type=(decision.kind.value if decision else str(input_summary.get("kind", "invalid"))),
            tool_name=outcome.tool_name, delegated_agent=outcome.delegated_agent, input_summary=bounded(input_summary),
            output_summary=bounded(outcome.output), status=outcome.step_status, latency_ms=outcome.latency_ms, created_at=now,
        )
        session.add(step)
        self._audit(session, actor, mission, "AGENT_STEP_EXECUTED", f"Mission {mission.id} step {step.step_number}: "
                    f"{step.action_type} ({step.status.value}).",
                    {"step_number": step.step_number, "action_type": step.action_type, "status": step.status.value,
                     "error_code": outcome.error_code}, now)
        return step

    def _reject(self, session: Session, actor: MissionActor, mission: AgentMission, rejected: _Rejected,
                now: datetime) -> AgentResult:
        outcome = _Outcome(AgentStepStatus.REJECTED, {"error_code": rejected.code}, error_code=rejected.code,
                           tool_name=rejected.summary.get("tool_name"), delegated_agent=rejected.summary.get("delegate_agent"))
        kind = rejected.summary.get("kind") or ("tool" if "tool_name" in rejected.summary else
                                                "delegate" if "delegate_agent" in rejected.summary else "invalid")
        step = self._record_step(session, actor, mission, None, outcome, now, rejected_summary={"kind": kind, **rejected.summary})
        self._finish(session, actor, mission, AgentMissionStatus.FAILED, now, "MISSION_FAILED",
                     f"Mission {mission.id} failed: decision rejected ({rejected.code}).", {"code": rejected.code})
        return self._commit(session, mission, step, None, rejected.code)

    def _record_crash(self, session: Session, actor: MissionActor, mission_id: int, decision: AgentDecision,
                      crashed: "_ToolCrashed", now: datetime) -> AgentResult:
        session.rollback()  # discard anything the crashed tool may have staged
        mission = self._load(session, actor, mission_id, owner_only=True)
        outcome = _Outcome(AgentStepStatus.FAILED, {"ok": False, "error_code": "TOOL_CRASHED", "error_type": crashed.error_type},
                           tool_name=crashed.tool_name, error_code="TOOL_CRASHED")
        step = self._record_step(session, actor, mission, decision, outcome, now)
        self._finish(session, actor, mission, AgentMissionStatus.FAILED, now, "MISSION_FAILED",
                     f"Mission {mission.id} failed: tool {crashed.tool_name} crashed.", {"code": "TOOL_CRASHED"})
        return self._commit(session, mission, step, decision.kind, "TOOL_CRASHED")

    @staticmethod
    def _commit(session: Session, mission: AgentMission, step: AgentStep, kind: Optional[DecisionKind],
                error_code: Optional[str]) -> AgentResult:
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise AgentOSError("CONCURRENT_STEP", "Another run of this mission step was recorded first.", 409) from None
        return AgentResult(mission_id=mission.id, transitioned=True, status=mission.status, step_number=step.step_number,
                           decision_kind=kind, step_status=step.status, error_code=error_code)


class _ToolCrashed(Exception):
    def __init__(self, tool_name: str, error_type: str) -> None:
        super().__init__(tool_name)
        self.tool_name, self.error_type = tool_name, error_type


def _decision_summary(decision: AgentDecision) -> Dict[str, Any]:
    """The observable decision, field by field from the typed model -- nothing free-form is copied wholesale."""
    summary: Dict[str, Any] = {"kind": decision.kind.value}
    if decision.kind == DecisionKind.TOOL:
        summary.update(tool_name=decision.tool_name, tool_input=decision.tool_input)
    elif decision.kind == DecisionKind.DELEGATE:
        summary.update(delegate_agent=decision.delegate_agent, delegate_goal=decision.delegate_goal)
    elif decision.kind == DecisionKind.WAIT:
        summary.update(wait_for=decision.wait_for.value, wake_after_seconds=decision.wake_after_seconds)
    elif decision.kind == DecisionKind.ASK_HUMAN:
        summary.update(question=decision.question)
    elif decision.kind == DecisionKind.REPLAN:
        summary.update(plan=decision.plan)
    elif decision.kind == DecisionKind.COMPLETE:
        summary.update(outcome=decision.outcome)
    else:
        summary.update(reason=decision.reason)
    return summary
