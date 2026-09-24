"""POST /missions, GET /missions/{id}, GET /missions/{id}/timeline,
GET /missions/{id}/evidence, POST /missions/{id}/resume, and (Phase 13)
GET /missions/{id}/candidates, POST /missions/{id}/candidates/refresh,
POST /missions/{id}/selection.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import Identity, get_identity, get_orchestrator, get_session
from app.api.schemas.missions import (
    AgentRunView,
    ApprovalSummaryView,
    CandidateListResponse,
    ExecutionStopView,
    MissionCreateRequest,
    MissionEvidenceResponse,
    MissionResponse,
    MissionTaskView,
    MissionTimelineResponse,
    SelectionRequest,
    SelectionResponse,
    TaskEvidenceView,
)
from app.api.timeline import build_timeline
from app.db.models.mission import Mission
from app.db.repositories.missions import (
    get_audit_trail,
    get_mission_steps,
    list_approvals_by_status,
    list_approvals_for_step,
    list_pending_approvals,
)
from app.graph.orchestrator import (
    DUPLICATE_FAILURE_MESSAGE,
    MissionOrchestrator,
    SelectionError,
    selection_locked,
    user_selection_required,
)
from app.schemas.enums import ApprovalStatus, UserRole
from app.schemas.evidence import Evidence
from app.schemas.selection import SelectionResult
from app.services import target_selection
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
    selections = context.list_active_target_selections(mission.mission_id)
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
    agent_results: List[AgentRunView] = []
    last_run_by_task: dict = {}
    for r in runs:
        view = AgentRunView(
            task_id=r.step_id, agent=r.agent.value, status=r.status.value,
            facts={k: v for k, v in (r.facts or {}).items() if k != "_response_text"},
            errors=list(r.errors or []),
            evidence=[Evidence.model_validate(e) for e in (r.evidence or [])],
            response_text=str((r.facts or {}).get("_response_text") or ""),
        )
        previous = last_run_by_task.get(r.step_id)
        view.identical_to_previous_run = previous is not None and (
            (previous.status, previous.errors, previous.response_text) == (view.status, view.errors, view.response_text)
        )
        last_run_by_task[r.step_id] = view
        agent_results.append(view)

    pending = list_pending_approvals(session, mission_id=mission.mission_id)
    pending_views = [
        ApprovalSummaryView(approval_id=a.approval_id, step_id=a.step_id, action_summary=a.action_summary, status=a.status.value)
        for a in pending
    ]
    stale_views = [
        ApprovalSummaryView(
            approval_id=a.approval_id, step_id=a.step_id, action_summary=a.action_summary, status=a.status.value,
            invalidation_reason=a.invalidation_reason, replaced_by_approval_id=_next_approval_id(session, a),
        )
        for a in list_approvals_by_status(session, ApprovalStatus.STALE, mission_id=mission.mission_id)
    ]

    return MissionResponse(
        mission_id=mission.mission_id, goal=mission.original_goal, status=mission.status.value,
        plan=plan_view, agent_results=agent_results, pending_approvals=pending_views, stale_approvals=stale_views,
        final_result=mission.final_result, execution_stop=_execution_stop(context, mission.mission_id),
        user_selection_required=user_selection_required(plan, mission.original_goal, selections),
        selected_target=next(iter(selections.values()), None),
        created_at=mission.created_at, updated_at=mission.updated_at,
    )


def _next_approval_id(session: Session, approval) -> Optional[str]:
    """The request that replaced ``approval`` for the same step, if any."""
    chain = [a.approval_id for a in list_approvals_for_step(session, approval.step_id)]
    index = chain.index(approval.approval_id)
    return chain[index + 1] if index + 1 < len(chain) else None


def _execution_stop(context: ContextService, mission_id: str) -> Optional[ExecutionStopView]:
    stop = next(
        (e for e in reversed(context.list_audit_events(mission_id)) if e.event_type == "duplicate_failure_detected"),
        None,
    )
    if stop is None:
        return None
    return ExecutionStopView(
        reason="duplicate_failure",
        message=DUPLICATE_FAILURE_MESSAGE,
        task_ids=list((stop.event_metadata or {}).get("task_ids") or []),
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


# ---------------------------------------------------------------------------
# Phase 13: candidate selection
# ---------------------------------------------------------------------------


def _candidate_list(session: Session, mission: Mission) -> CandidateListResponse:
    context = ContextService(session)
    plan = context.get_latest_plan_snapshot(mission.mission_id)
    selections = context.list_active_target_selections(mission.mission_id)
    required = user_selection_required(plan, mission.original_goal, selections)
    candidates = target_selection.list_candidates(session, mission.mission_id)
    selection_open = bool(candidates) and (required or any(not selection_locked(session, s) for s in selections.values()))
    return CandidateListResponse(
        mission_id=mission.mission_id, user_selection_required=required, selection_open=selection_open,
        selected_target=next(iter(selections.values()), None), candidates=candidates,
    )


@router.get("/missions/{mission_id}/candidates", response_model=CandidateListResponse)
def get_candidates(mission_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(get_session)) -> CandidateListResponse:
    mission = _load_mission_or_404(session, mission_id)
    _check_mission_ownership(identity, mission)
    return _candidate_list(session, mission)


@router.post("/missions/{mission_id}/candidates/refresh", response_model=CandidateListResponse)
def refresh_candidates(
    mission_id: str, identity: Identity = Depends(get_identity), session: Session = Depends(get_session)
) -> CandidateListResponse:
    """Re-check every candidate against current data (capacity, deadline,
    registrations, timetable, exams). Deterministic: no LLM call, no new mission."""
    mission = _load_mission_or_404(session, mission_id)
    _check_mission_ownership(identity, mission)
    candidates = target_selection.refresh_candidates(
        session, mission_id=mission_id, student_id=mission.user_id, now=datetime.now(timezone.utc)
    )
    ContextService(session).append_audit_event(
        event_id=f"evt-{uuid.uuid4().hex[:12]}", mission_id=mission_id, event_type="action_candidates_refreshed",
        actor=identity.key, message=f"Re-checked {len(candidates)} candidate(s) against current data.",
        metadata={"statuses": {str(c.resource_id): c.assessment.status.value for c in candidates}},
    )
    return _candidate_list(session, mission)


@router.post("/missions/{mission_id}/selection", response_model=SelectionResponse)
def select_candidate(
    mission_id: str,
    body: SelectionRequest,
    identity: Identity = Depends(get_identity),
    orchestrator: MissionOrchestrator = Depends(get_orchestrator),
    session: Session = Depends(get_session),
) -> SelectionResponse:
    """The student picks one candidate as the action's target.

    Only the mission's own student may select -- selecting is the student's
    decision, distinct from an approver's. The request carries only the
    resource's identity; the server reloads and re-validates everything else.
    """
    mission = _load_mission_or_404(session, mission_id)
    if identity.role != UserRole.STUDENT:
        raise HTTPException(status_code=403, detail="Only the mission's student may select an action target.")
    _check_mission_ownership(identity, mission)
    try:
        outcome = orchestrator.select_target(
            mission_id, tool_name=body.action, resource_type=body.resource_type, resource_id=body.resource_id,
            selected_by=identity.student_id or identity.key,
        )
    except SelectionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if outcome.result == SelectionResult.BLOCKED:
        raise HTTPException(status_code=409, detail=outcome.message)
    session.expire_all()
    mission = _load_mission_or_404(session, mission_id)
    return SelectionResponse(
        result=outcome.result.value, message=outcome.message, selection=outcome.selection, candidate=outcome.candidate,
        superseded_approval_id=outcome.superseded_approval_id, mission=_mission_response(session, mission),
    )
