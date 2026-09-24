"""Phase 13: action candidates and their deterministic selectability.

A deterministic service (no LLM, no planning): it turns the verified Events
Agent result of a mission into persisted, selectable candidates, re-assesses
them against current data on refresh and on selection, and renders them for
the API. Selectability comes only from ``check_event_registration`` (the same
rule the Action Agent's pre-check and execute-time recheck run) mapped by
``app.rules.candidate_status`` -- never from an agent's prose or an LLM.

Schedule data: at discovery the candidates are checked against the mission's
own *verified* Academic timetable and exam results. A refresh or a selection
re-checks against the student's *current* timetable and exams, because the
earlier results may be stale by then.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.db.models.mission import ActionCandidateRecord
from app.db.repositories.students import get_student_by_id
from app.rules.action_preconditions import check_event_registration
from app.rules.candidate_status import derive_candidate_status
from app.rules.event_availability import find_exam_conflicts, find_timetable_conflicts
from app.schemas.academic import ExamEntry, TimetableEntry
from app.schemas.enums import AgentName, AgentResultStatus, TaskStatus
from app.schemas.events import EventSummary
from app.schemas.mission import MissionPlan
from app.schemas.selection import (
    ACTION_RESOURCE_TYPES,
    SELECTABLE_ACTIONS,
    ActionCandidate,
    CandidateAssessment,
    CandidateStatus,
    SelectedTarget,
)
from app.services import academic as academic_service
from app.services import events as events_service
from app.services.context import ContextService

SCHEDULE_FROM_MISSION = "verified_academic_tasks"
SCHEDULE_CURRENT = "current_student_schedule"

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class MissionSchedule:
    """The student's timetable + exams as verified by this mission's Academic tasks."""

    timetable: List[TimetableEntry]
    exams: List[ExamEntry]
    step_ids: List[str]


def _latest_runs(context: ContextService, mission_id: str) -> Dict[str, Any]:
    latest: Dict[str, Any] = {}
    for run in context.list_agent_runs(mission_id):
        latest[run.step_id] = run
    return latest


def _completed_runs(context: ContextService, mission_id: str, plan: MissionPlan) -> List[Tuple[Any, Any]]:
    """``(task, run)`` for every task of the current plan that is COMPLETED
    with a successful (verified) latest run, in plan order."""
    mission = context.get_mission(mission_id)
    step_status = {step.step_id: step.status for step in mission.steps} if mission is not None else {}
    latest = _latest_runs(context, mission_id)
    pairs = []
    for task in plan.tasks:
        run = latest.get(task.task_id)
        if run is not None and step_status.get(task.task_id) == TaskStatus.COMPLETED and run.status == AgentResultStatus.SUCCESS:
            pairs.append((task, run))
    return pairs


def verified_schedule(context: ContextService, mission_id: str, plan: MissionPlan) -> Optional[MissionSchedule]:
    """The full timetable and exam list from this mission's verified Academic
    tasks, or None when either is missing (a partial scope does not count)."""
    timetable: Optional[List[TimetableEntry]] = None
    exams: Optional[List[ExamEntry]] = None
    step_ids: List[str] = []
    for task, run in _completed_runs(context, mission_id, plan):
        if task.agent != AgentName.ACADEMIC_AGENT:
            continue
        facts = run.facts or {}
        if timetable is None and facts.get("timetable") is not None and facts.get("timetable_scope", "all") == "all":
            timetable = [TimetableEntry.model_validate(item) for item in facts["timetable"]]
            step_ids.append(task.task_id)
        if exams is None and facts.get("exams") is not None and facts.get("exams_scope", "all") == "all":
            exams = [ExamEntry.model_validate(item) for item in facts["exams"]]
            if task.task_id not in step_ids:
                step_ids.append(task.task_id)
    if timetable is None or exams is None:
        return None
    return MissionSchedule(timetable=timetable, exams=exams, step_ids=step_ids)


def _conflict_reasons(assessment: CandidateAssessment) -> List[str]:
    reasons = [
        f"Conflicts with {c.course_code} class ({_WEEKDAYS[c.weekday]} {c.start_time}-{c.end_time})."
        for c in assessment.timetable_conflicts
    ]
    reasons += [
        f"Conflicts with {c.course_code} {c.exam_type} exam ({c.scheduled_start.isoformat()})."
        for c in assessment.exam_conflicts
    ]
    return reasons


def assess_event_candidate(
    session: Session,
    *,
    student_id: str,
    event_id: int,
    now: datetime,
    timetable: Optional[List[TimetableEntry]],
    exams: Optional[List[ExamEntry]],
    schedule_source: Optional[str],
) -> Tuple[Optional[EventSummary], CandidateAssessment]:
    """Re-derive one event candidate's status from current DB state."""
    student_exists = get_student_by_id(session, student_id) is not None
    event = events_service.get_event_summary(session, event_id) if student_exists else None
    already_registered = False
    confirmed = 0
    schedule_checked = event is not None and timetable is not None and exams is not None
    timetable_conflicts, exam_conflicts = [], []
    if event is not None:
        already_registered = events_service.get_student_registration_status(session, student_id, event.event_id) is not None
        confirmed = events_service.get_registration_count(session, event.event_id)
        if schedule_checked:
            timetable_conflicts = find_timetable_conflicts(event, timetable)
            exam_conflicts = find_exam_conflicts(event, exams)
    checks = check_event_registration(
        student_exists=student_exists,
        event=event,
        confirmed_registrations=confirmed,
        already_registered=already_registered,
        now=now,
        timetable_conflicts=timetable_conflicts,
        exam_conflicts=exam_conflicts,
        conflict_check_performed=schedule_checked,
    )
    status, reasons = derive_candidate_status(checks)
    assessment = CandidateAssessment(
        status=status,
        reasons=reasons,
        schedule_checked=schedule_checked,
        schedule_source=schedule_source if schedule_checked else None,
        timetable_conflicts=timetable_conflicts,
        exam_conflicts=exam_conflicts,
        confirmed_registrations=confirmed if event is not None else None,
        assessed_at=now,
    )
    if status == CandidateStatus.CONFLICT:
        assessment.reasons = [reasons[0], *_conflict_reasons(assessment)]
    elif status == CandidateStatus.NEEDS_REVIEW and event is not None and not schedule_checked:
        assessment.reasons = [reasons[0], "Your timetable and exams were not verified in this mission."]
    return event, assessment


def _recommendation(raw: Dict[str, Any], evidence_refs: List[str]) -> Dict[str, Any]:
    gaps = list(raw.get("matched_skill_gaps") or [])
    terms = list(raw.get("matched_terms") or [])
    if gaps:
        reason = "Matches your skill gap(s): " + ", ".join(gaps) + "."
    elif terms:
        reason = "Matches your request: " + ", ".join(terms) + "."
    else:
        reason = "Found by the Events & Opportunity Agent for your request."
    return {"reason": reason, "matched_terms": terms, "matched_skill_gaps": gaps, "evidence_refs": evidence_refs}


def record_candidates(
    session: Session,
    *,
    mission_id: str,
    plan: MissionPlan,
    tools: Iterable[str],
    student_id: str,
    now: datetime,
) -> List[ActionCandidate]:
    """Persist the candidates for every selectable action the mission is waiting on.

    Candidates come only from verified Events Agent results of the current
    plan; each is assessed against the mission's verified schedule. Re-running
    this updates the same rows (one per mission/tool/resource).
    """
    context = ContextService(session)
    wanted = [tool for tool in dict.fromkeys(tools) if tool in SELECTABLE_ACTIONS]
    if not wanted:
        return []
    schedule = verified_schedule(context, mission_id, plan)
    sequence = 0
    for tool_name in wanted:
        seen: set = set()
        for task, run in _completed_runs(context, mission_id, plan):
            if task.agent != AgentName.EVENTS_OPPORTUNITY_AGENT:
                continue
            evidence_refs = [str(e.get("evidence_id")) for e in (run.evidence or []) if e.get("evidence_id")]
            for raw in (run.facts or {}).get("assessments") or []:
                event_id = (raw.get("event") or {}).get("event_id")
                if not isinstance(event_id, int) or event_id in seen:
                    continue
                seen.add(event_id)
                event, assessment = assess_event_candidate(
                    session, student_id=student_id, event_id=event_id, now=now,
                    timetable=schedule.timetable if schedule else None,
                    exams=schedule.exams if schedule else None,
                    schedule_source=SCHEDULE_FROM_MISSION,
                )
                recommendation = _recommendation(raw, evidence_refs)
                recommendation["mission_schedule_verified"] = schedule is not None
                recommendation["schedule_step_ids"] = schedule.step_ids if schedule else []
                context.upsert_action_candidate(
                    mission_id=mission_id, tool_name=tool_name, resource_type=ACTION_RESOURCE_TYPES[tool_name],
                    resource_id=event_id,
                    title=event.title if event is not None else str((raw.get("event") or {}).get("title") or event_id),
                    status=assessment.status.value, assessment=assessment.model_dump(mode="json"), assessed_at=now,
                    recommendation=recommendation, recommended_by_step_id=task.task_id, sequence=sequence,
                )
                sequence += 1
    return list_candidates(session, mission_id)


def reassess_candidate(
    session: Session, record: ActionCandidateRecord, *, student_id: str, now: datetime
) -> CandidateAssessment:
    """Re-check one persisted candidate against *current* data and store the result.

    The schedule is re-read from the student's current timetable and exams --
    but only when this mission verified the schedule in the first place.
    """
    use_schedule = bool((record.recommendation or {}).get("mission_schedule_verified"))
    timetable = academic_service.get_timetable(session, student_id) if use_schedule else None
    exams = academic_service.get_exam_schedule(session, student_id) if use_schedule else None
    event, assessment = assess_event_candidate(
        session, student_id=student_id, event_id=record.resource_id, now=now,
        timetable=timetable, exams=exams, schedule_source=SCHEDULE_CURRENT,
    )
    ContextService(session).upsert_action_candidate(
        mission_id=record.mission_id, tool_name=record.tool_name, resource_type=record.resource_type,
        resource_id=record.resource_id, title=event.title if event is not None else record.title,
        status=assessment.status.value, assessment=assessment.model_dump(mode="json"), assessed_at=now,
    )
    return assessment


def refresh_candidates(session: Session, *, mission_id: str, student_id: str, now: datetime) -> List[ActionCandidate]:
    """Re-assess every candidate of a mission against current data (no LLM)."""
    for record in ContextService(session).list_action_candidates(mission_id):
        reassess_candidate(session, record, student_id=student_id, now=now)
    return list_candidates(session, mission_id)


def to_candidate(record: ActionCandidateRecord, selections: Dict[str, SelectedTarget], session: Session) -> ActionCandidate:
    assessment = CandidateAssessment.model_validate(record.assessment)
    recommendation = record.recommendation or {}
    event = events_service.get_event_summary(session, record.resource_id)
    seats: Optional[int] = None
    if event is not None and event.capacity is not None and assessment.confirmed_registrations is not None:
        seats = max(event.capacity - assessment.confirmed_registrations, 0)
    selected = any(
        s.tool_name == record.tool_name and s.resource_type == record.resource_type and s.resource_id == record.resource_id
        for s in selections.values()
    )
    return ActionCandidate(
        mission_id=record.mission_id,
        tool_name=record.tool_name,
        resource_type=record.resource_type,
        resource_id=record.resource_id,
        title=record.title,
        start_at=event.start_at if event else None,
        end_at=event.end_at if event else None,
        venue=event.location if event else None,
        registration_deadline=event.registration_deadline if event else None,
        capacity=event.capacity if event else None,
        seats_remaining=seats,
        recommended_by_step_id=record.recommended_by_step_id,
        recommendation_reason=str(recommendation.get("reason") or ""),
        matched_terms=list(recommendation.get("matched_terms") or []),
        matched_skill_gaps=list(recommendation.get("matched_skill_gaps") or []),
        evidence_refs=list(recommendation.get("evidence_refs") or []),
        assessment=assessment,
        selectable=assessment.selectable,
        selected=selected,
    )


def list_candidates(session: Session, mission_id: str) -> List[ActionCandidate]:
    context = ContextService(session)
    selections = context.list_active_target_selections(mission_id)
    return [to_candidate(record, selections, session) for record in context.list_action_candidates(mission_id)]
