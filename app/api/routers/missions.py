"""POST /missions, GET /missions/{id}, GET /missions/{id}/timeline,
GET /missions/{id}/evidence, POST /missions/{id}/resume.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import Identity, get_identity, get_orchestrator, get_session
from app.api.schemas.missions import (
    AgentRunView,
    ApprovalSummaryView,
    MissionCreateRequest,
    MissionEvidenceResponse,
    MissionResponse,
    MissionTaskView,
    MissionTimelineResponse,
    TaskEvidenceView,
)
from app.api.timeline import build_timeline
from app.db.models.mission import Mission
from app.db.repositories.missions import get_audit_trail, get_mission_steps, list_pending_approvals
from app.graph.orchestrator import MissionOrchestrator
from app.schemas.enums import UserRole
from app.schemas.evidence import Evidence
from app.services.context import ContextService

router = APIRouter(tags=["missions"])


def _load_mission_or_404(session: Session, mission_id: str) -> Mission:
    mission = session.get(Mission, mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail=f"unknown mission_id {mission_id!r}")
    return mission


def _check_mission_ownership(identity: Identity, mission: Mission) -> None:
    if identity.role == UserRole.STUDENT and mission.user_id != identity.student_id:
        raise HTTPException(status_code=403, detail="students may only access their own missions.")


def _mission_response(session: Session, mission: Mission) -> MissionResponse:
    context = ContextService(session)
    plan = context.get_latest_plan_snapshot(mission.mission_id)
    task_by_id = {t.task_id: t for t in plan.tasks} if plan is not None else {}
    steps = sorted(mission.steps, key=lambda s: s.sequence)

    plan_view = [
        MissionTaskView(
            task_id=step.step_id,
            agent=step.agent.value,
            objective=task_by_id[step.step_id].objective if step.step_id in task_by_id else step.objective,
            dependencies=list(task_by_id[step.step_id].dependencies) if step.step_id in task_by_id else [],
            status=step.status.value,
        )
        for step in steps
    ]

    runs = context.list_agent_runs(mission.mission_id)
    agent_results = [
        AgentRunView(
            task_id=r.step_id, agent=r.agent.value, status=r.status.value,
            facts={k: v for k, v in (r.facts or {}).items() if k != "_response_text"},
            errors=list(r.errors or []),
            evidence=[Evidence.model_validate(e) for e in (r.evidence or [])],
            response_text=str((r.facts or {}).get("_response_text") or ""),
        )
        for r in runs
    ]

    pending = list_pending_approvals(session, mission_id=mission.mission_id)
    pending_views = [
        ApprovalSummaryView(approval_id=a.approval_id, step_id=a.step_id, action_summary=a.action_summary, status=a.status.value)
        for a in pending
    ]

    return MissionResponse(
        mission_id=mission.mission_id, goal=mission.original_goal, status=mission.status.value,
        plan=plan_view, agent_results=agent_results, pending_approvals=pending_views,
        final_result=mission.final_result, created_at=mission.created_at, updated_at=mission.updated_at,
    )


@router.post("/missions", response_model=MissionResponse)
def create_mission(
    body: MissionCreateRequest,
    identity: Identity = Depends(get_identity),
    orchestrator: MissionOrchestrator = Depends(get_orchestrator),
    session: Session = Depends(get_session),
) -> MissionResponse:
    if identity.role == UserRole.STUDENT:
        if body.student_id is not None and body.student_id != identity.student_id:
            raise HTTPException(status_code=400, detail="students may only start missions for themselves.")
        effective_student_id = identity.student_id
    else:
        effective_student_id = body.student_id or identity.student_id
        if effective_student_id is None:
            raise HTTPException(status_code=400, detail="student_id is required for a staff-initiated mission.")

    final_state = orchestrator.run_mission(
        body.goal, user_id=effective_student_id, user_role=UserRole.STUDENT, student_id=effective_student_id
    )
    mission = _load_mission_or_404(session, final_state["mission_id"])
    return _mission_response(session, mission)


@router.get("/missions/{mission_id}", response_model=MissionResponse)
def get_mission_detail(mission_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(get_session)) -> MissionResponse:
    mission = _load_mission_or_404(session, mission_id)
    _check_mission_ownership(identity, mission)
    return _mission_response(session, mission)


@router.get("/missions/{mission_id}/timeline", response_model=MissionTimelineResponse)
def get_mission_timeline(mission_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(get_session)) -> MissionTimelineResponse:
    mission = _load_mission_or_404(session, mission_id)
    _check_mission_ownership(identity, mission)
    steps = get_mission_steps(session, mission_id)
    audit_events = get_audit_trail(session, mission_id)
    return MissionTimelineResponse(mission_id=mission_id, entries=build_timeline(steps, audit_events))


@router.get("/missions/{mission_id}/evidence", response_model=MissionEvidenceResponse)
def get_mission_evidence(mission_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(get_session)) -> MissionEvidenceResponse:
    mission = _load_mission_or_404(session, mission_id)
    _check_mission_ownership(identity, mission)
    context = ContextService(session)
    tasks = [
        TaskEvidenceView(task_id=r.step_id, agent=r.agent.value, evidence=[Evidence.model_validate(e) for e in (r.evidence or [])])
        for r in context.list_agent_runs(mission_id)
    ]
    return MissionEvidenceResponse(mission_id=mission_id, tasks=tasks)


@router.post("/missions/{mission_id}/resume", response_model=MissionResponse)
def resume_mission(
    mission_id: str,
    identity: Identity = Depends(get_identity),
    orchestrator: MissionOrchestrator = Depends(get_orchestrator),
    session: Session = Depends(get_session),
) -> MissionResponse:
    mission = _load_mission_or_404(session, mission_id)
    _check_mission_ownership(identity, mission)
    orchestrator.resume_mission(mission_id)
    session.expire_all()
    mission = _load_mission_or_404(session, mission_id)
    return _mission_response(session, mission)
