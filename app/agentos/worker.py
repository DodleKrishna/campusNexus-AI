"""Due-mission worker (AgentOS V2 Phase 3): advances autonomous missions whose wake time has come.

``process_due_missions`` is reusable for every autonomous agent (the Assignment, Exam and Attendance
Guardians). One call:

1. lists active organizations, and in each one's *tenant* session the due missions of
   autonomous agents (``AgentSpec.autonomous``): not terminal, ``next_wake_at <= now``, no live lease;
2. claims each one with a single conditional UPDATE (lease owner + expiry). On SQLite and
   PostgreSQL alike only one worker's UPDATE matches (PostgreSQL re-checks the WHERE after the row
   lock), so two workers never process the same mission; a crashed worker's lease expires;
3. rebuilds the mission owner's identity from their *active* membership and runs ``run_until_blocked``
   with a small transition cap. An owner without an active account/membership is never impersonated:
   the mission is parked in WAITING_HUMAN (``waiting_for = OWNER_INACTIVE``, no wake time) and audited
   OWNER_INACTIVE_MISSION_PARKED once -- no retry every few minutes. A later wake (a domain event) re-checks
   the owner: still inactive re-parks silently; active again resumes normally;
4. releases the lease, pushes the wake time forward when the mission is still RUNNING (cap reached)
   or the brain was unavailable (backoff, no retry storm), and audits MISSION_WORKER_RUN.

Bounded by construction: at most ``limit`` missions per call, each processed at most once, each for at
most ``max_transitions`` transitions. Nothing here loops until a condition holds.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, List, Literal, Optional

from pydantic import BaseModel
from sqlalchemy import or_, select, update

from app.agentos.runtime import HARD_TRANSITION_CAP, SUBJECT, AgentOSError, AgentRuntime, MissionActor
from app.db.models.agent_kernel import TERMINAL_MISSION_STATUSES, AgentMission, AgentMissionStatus
from app.db.models.auth import AuthAccount
from app.db.models.identity import Student
from app.db.models.organization import Organization
from app.db.repositories import operations_audit
from app.db.tenancy import active_membership
from app.db.tenant_session import TenantSessionFactory
from app.llm.router import ai_context
from app.schemas.enums import OrganizationStatus

MAX_BATCH = 100
DEFAULT_LIMIT = 10
DEFAULT_TRANSITIONS = 4
LEASE = timedelta(minutes=5)
RUNNING_RETRY = timedelta(minutes=5)  # still RUNNING after the cap: continue on a later run
UNAVAILABLE_BACKOFF = timedelta(minutes=10)  # brain unavailable / worker error
OWNER_INACTIVE = "OWNER_INACTIVE"  # ``waiting_for`` of a parked mission (never a published event type)
ACTIVE_STATUSES = [s for s in AgentMissionStatus if s not in TERMINAL_MISSION_STATUSES]


class MissionRun(BaseModel):
    organization_id: int
    mission_id: int
    agent_key: str
    outcome: Literal["processed", "skipped", "not_claimed", "error"]
    transitions: int = 0
    status: Optional[str] = None
    error_code: Optional[str] = None


class WorkerReport(BaseModel):
    worker_id: str
    due: int = 0
    claimed: int = 0
    runs: List[MissionRun] = []


def _autonomous_keys(runtime: AgentRuntime) -> List[str]:
    return [key for key in runtime.agents.keys() if (spec := runtime.agents.get(key)) is not None and spec.autonomous]


def claim(session: Any, mission_id: int, worker_id: str, now: datetime, lease: timedelta = LEASE) -> bool:
    """Take the mission's lease atomically (one conditional UPDATE in the caller's tenant session). True if taken."""
    result = session.execute(
        update(AgentMission).where(
            AgentMission.id == mission_id, AgentMission.status.in_(ACTIVE_STATUSES),
            AgentMission.next_wake_at.is_not(None), AgentMission.next_wake_at <= now,
            or_(AgentMission.lease_expires_at.is_(None), AgentMission.lease_expires_at <= now),
        ).values(lease_owner=worker_id, lease_expires_at=now + lease).execution_options(synchronize_session=False))
    session.commit()
    return result.rowcount == 1


def _owner_actor(session: Any, organization_id: int, mission: AgentMission) -> Optional[MissionActor]:
    account = session.get(AuthAccount, mission.owner_account_id)
    membership = active_membership(session, mission.owner_account_id)  # tenant-filtered: this organization only
    if account is None or not account.is_active or membership is None:
        return None
    student = session.get(Student, membership.student_id) if membership.student_id is not None else None
    return MissionActor(organization_id=organization_id, account_id=account.id, role=membership.role,
                        student_code=student.student_code if student is not None else None,
                        faculty_profile_id=membership.faculty_profile_id)


def process_due_missions(
    factory: TenantSessionFactory, runtime: AgentRuntime, *, limit: int = DEFAULT_LIMIT,
    max_transitions: int = DEFAULT_TRANSITIONS, now: Optional[datetime] = None, worker_id: Optional[str] = None,
    recorder: Any = None,
) -> WorkerReport:
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f"limit must be 1-{MAX_BATCH}")
    if not 1 <= max_transitions <= HARD_TRANSITION_CAP:
        raise ValueError(f"max_transitions must be 1-{HARD_TRANSITION_CAP}")
    factory = TenantSessionFactory.from_sessionmaker(factory)
    now = now or runtime.clock()
    report = WorkerReport(worker_id=(worker_id or f"worker-{uuid.uuid4().hex[:12]}")[:64])
    agent_keys = _autonomous_keys(runtime)
    if not agent_keys:
        return report
    with factory() as unbound:  # organizations are global; no tenant data is read here
        organization_ids = list(unbound.execute(select(Organization.id).where(
            Organization.status == OrganizationStatus.ACTIVE).order_by(Organization.id)).scalars())
    for organization_id in organization_ids:
        remaining = limit - report.due
        if remaining <= 0:
            break
        with factory.open_tenant_session(organization_id) as session:
            due = list(session.execute(select(AgentMission.id, AgentMission.agent_key).where(
                AgentMission.agent_key.in_(agent_keys), AgentMission.status.in_(ACTIVE_STATUSES),
                AgentMission.next_wake_at.is_not(None), AgentMission.next_wake_at <= now,
                or_(AgentMission.lease_expires_at.is_(None), AgentMission.lease_expires_at <= now),
            ).order_by(AgentMission.next_wake_at, AgentMission.id).limit(remaining)).all())
        report.due += len(due)
        for mission_id, agent_key in due:
            report.runs.append(_process_one(factory, runtime, organization_id, mission_id, agent_key, now,
                                            report.worker_id, max_transitions, recorder))
    report.claimed = sum(1 for r in report.runs if r.outcome != "not_claimed")
    return report


def _process_one(factory: TenantSessionFactory, runtime: AgentRuntime, organization_id: int, mission_id: int,
                 agent_key: str, now: datetime, worker_id: str, max_transitions: int, recorder: Any) -> MissionRun:
    run = MissionRun(organization_id=organization_id, mission_id=mission_id, agent_key=agent_key, outcome="processed")
    with factory.open_tenant_session(organization_id) as session:
        if not claim(session, mission_id, worker_id, now):
            run.outcome = "not_claimed"
            return run
        mission = session.get(AgentMission, mission_id)
        actor = _owner_actor(session, organization_id, mission)
        backoff = False
        if actor is None:
            run.outcome, run.error_code = "skipped", OWNER_INACTIVE
            _park_owner_inactive(session, mission, agent_key, now)
        else:
            try:
                with ai_context(organization_id, mission_id=f"agentos:{mission_id}", recorder=recorder, agent_key=agent_key):
                    results = runtime.run_until_blocked(session, actor, mission_id, max_transitions=max_transitions)
                run.transitions = sum(1 for r in results if r.transitioned)
                last = results[-1] if results else None
                if last is not None and not last.transitioned and last.error_code:
                    run.error_code, backoff = last.error_code, True  # e.g. brain unavailable: mission unchanged
            except AgentOSError as exc:
                session.rollback()
                run.outcome, run.error_code, backoff = "error", exc.code, True
            except Exception as exc:  # noqa: BLE001 -- one broken mission never stops the batch
                session.rollback()
                run.outcome, run.error_code, backoff = "error", f"WORKER_ERROR:{type(exc).__name__}"[:64], True
        session.expire_all()
        mission = session.get(AgentMission, mission_id)
        if mission.status not in TERMINAL_MISSION_STATUSES:
            if mission.status == AgentMissionStatus.RUNNING:
                mission.next_wake_at = now + RUNNING_RETRY
            elif backoff and (mission.next_wake_at is None or mission.next_wake_at <= now):
                mission.next_wake_at = now + UNAVAILABLE_BACKOFF
        if mission.lease_owner == worker_id:
            mission.lease_owner, mission.lease_expires_at = None, None
        run.status = mission.status.value
        operations_audit.record(
            session, event_type="MISSION_WORKER_RUN", actor_account_id=None, actor_role="worker", subject_type=SUBJECT,
            subject_id=str(mission_id), at=now, message=f"Worker processed mission {mission_id} ({run.outcome}).",
            metadata={"agent_key": agent_key, "worker_id": worker_id, "outcome": run.outcome,
                      "transitions": run.transitions, "status": run.status, "error_code": run.error_code})
        session.commit()
    return run


def _park_owner_inactive(session: Any, mission: AgentMission, agent_key: str, now: datetime) -> None:
    """Park (never impersonate): WAITING_HUMAN on OWNER_INACTIVE with no wake time, audited only on the first park."""
    already_parked = mission.status == AgentMissionStatus.WAITING_HUMAN and mission.waiting_for == OWNER_INACTIVE
    mission.status, mission.waiting_for, mission.next_wake_at = AgentMissionStatus.WAITING_HUMAN, OWNER_INACTIVE, None
    if not already_parked:
        operations_audit.record(
            session, event_type="OWNER_INACTIVE_MISSION_PARKED", actor_account_id=None, actor_role="worker",
            subject_type=SUBJECT, subject_id=str(mission.id), at=now,
            message=f"Mission {mission.id} parked: its owner has no active membership.",
            metadata={"agent_key": agent_key, "status": mission.status.value, "waiting_for": OWNER_INACTIVE})
    session.flush()  # the caller expires the session next; keep the parked state
