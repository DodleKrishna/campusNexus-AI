"""The Context Service: the single source of truth for mission/session state,
agent outputs, approvals, and the audit trail (CLAUDE.md component 10).

This is a deterministic, non-agent service: no LLM calls, no planning, no
agent routing, no business-rule reasoning. It only reads and writes state via
plain typed methods, each committed as its own transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models.academic import AttendanceRecord, Enrollment
from app.db.models.career import Application
from app.db.models.events import Event, EventRegistration
from app.db.models.identity import Student
from app.db.models.mission import (
    ActionCandidateRecord,
    AgentRun,
    ApprovalRecord,
    AuditLog,
    MemoryRecord,
    Mission,
    MissionStep,
    TargetSelectionRecord,
    ToolCallRecord,
)
from app.db.models.services import CampusCase, CaseStatus
from app.schemas.enums import (
    AgentName,
    AgentResultStatus,
    ApprovalStatus,
    MissionStatus,
    TaskStatus,
    ToolAccessType,
    ToolExecutionStatus,
    UserRole,
)
from app.schemas.mission import MissionPlan
from app.schemas.selection import SelectedTarget

_RESOLVED_APPROVAL_STATUSES = {
    ApprovalStatus.APPROVED,
    ApprovalStatus.REJECTED,
    ApprovalStatus.EDIT_REQUIRED,
}


@dataclass
class StudentContext:
    """A compact, database-backed snapshot handed to future specialist agents.

    Deliberately not the entire database: a small, typed summary rather than
    every row touching the student.
    """

    student_code: str
    full_name: str
    department_code: str
    year: int
    semester: int
    cgpa: float
    interests: Optional[str]
    career_goal: Optional[str]
    course_codes: List[str] = field(default_factory=list)
    attendance: List[Dict[str, Any]] = field(default_factory=list)
    open_case_codes: List[str] = field(default_factory=list)
    upcoming_event_titles: List[str] = field(default_factory=list)
    application_count: int = 0


class ContextService:
    """Deterministic accessor for mission/session state.

    Each public method is its own committed transaction, so callers never
    have to reason about a partially-applied multi-step write.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Mission lifecycle
    # ------------------------------------------------------------------

    def create_mission(
        self,
        mission_id: str,
        user_id: str,
        user_role: UserRole,
        original_goal: str,
        normalized_goal: Optional[str] = None,
    ) -> Mission:
        mission = Mission(
            mission_id=mission_id,
            user_id=user_id,
            user_role=user_role,
            original_goal=original_goal,
            normalized_goal=normalized_goal,
            status=MissionStatus.PENDING,
        )
        self._session.add(mission)
        self._session.commit()
        self._session.refresh(mission)
        return mission

    def get_mission(self, mission_id: str) -> Optional[Mission]:
        return self._session.get(Mission, mission_id)

    def update_mission_status(
        self,
        mission_id: str,
        status: MissionStatus,
        final_result: Optional[str] = None,
    ) -> Mission:
        mission = self._session.get(Mission, mission_id)
        if mission is None:
            raise ValueError(f"unknown mission_id: {mission_id!r}")
        mission.status = status
        if final_result is not None:
            mission.final_result = final_result
        mission.updated_at = utc_now()
        self._session.commit()
        self._session.refresh(mission)
        return mission

    # ------------------------------------------------------------------
    # Mission steps
    # ------------------------------------------------------------------

    def create_mission_step(
        self,
        step_id: str,
        mission_id: str,
        agent: AgentName,
        objective: str,
        sequence: int = 0,
    ) -> MissionStep:
        step = MissionStep(
            step_id=step_id,
            mission_id=mission_id,
            agent=agent,
            objective=objective,
            sequence=sequence,
            status=TaskStatus.PENDING,
        )
        self._session.add(step)
        self._session.commit()
        self._session.refresh(step)
        return step

    def update_mission_step(
        self,
        step_id: str,
        status: Optional[TaskStatus] = None,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        agent: Optional[AgentName] = None,
        objective: Optional[str] = None,
    ) -> MissionStep:
        """``agent``/``objective`` change only when a replan reuses this step's
        id for a different task (Phase 12B)."""
        step = self._session.get(MissionStep, step_id)
        if step is None:
            raise ValueError(f"unknown step_id: {step_id!r}")
        if status is not None:
            step.status = status
        if agent is not None:
            step.agent = agent
        if objective is not None:
            step.objective = objective
        if started_at is not None:
            step.started_at = started_at
        if completed_at is not None:
            step.completed_at = completed_at
        self._session.commit()
        self._session.refresh(step)
        return step

    # ------------------------------------------------------------------
    # Agent results / tool calls
    # ------------------------------------------------------------------

    def record_agent_result(
        self,
        mission_id: str,
        step_id: str,
        agent: AgentName,
        status: AgentResultStatus,
        facts: Optional[Dict[str, Any]] = None,
        errors: Optional[List[str]] = None,
        evidence: Optional[List[Dict[str, Any]]] = None,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> AgentRun:
        run = AgentRun(
            mission_id=mission_id,
            step_id=step_id,
            agent=agent,
            status=status,
            facts=facts or {},
            errors=errors or [],
            evidence=evidence or [],
            started_at=started_at,
            completed_at=completed_at,
        )
        self._session.add(run)
        self._session.commit()
        self._session.refresh(run)
        return run

    def record_tool_call(
        self,
        tool_call_id: str,
        mission_id: str,
        step_id: str,
        tool_name: str,
        read_or_write: ToolAccessType,
        arguments: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        status: ToolExecutionStatus = ToolExecutionStatus.PENDING,
        result_data: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        postcondition_verified: Optional[bool] = None,
        executed_at: Optional[datetime] = None,
    ) -> ToolCallRecord:
        """Record a tool call, deduping on ``idempotency_key`` when given.

        A retried mission step that re-issues the same idempotency key gets
        back the existing record rather than a duplicate row -- the DB-level
        half of the Idempotency Requirement in CLAUDE.md (the Action Agent
        still owns actually skipping re-execution).
        """
        if idempotency_key:
            existing = self._session.execute(
                select(ToolCallRecord).where(ToolCallRecord.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            if existing is not None:
                return existing

        record = ToolCallRecord(
            tool_call_id=tool_call_id,
            mission_id=mission_id,
            step_id=step_id,
            tool_name=tool_name,
            read_or_write=read_or_write,
            arguments=arguments or {},
            idempotency_key=idempotency_key,
            status=status,
            result_data=result_data,
            error=error,
            postcondition_verified=postcondition_verified,
            executed_at=executed_at,
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return record

    def update_tool_call_record(
        self,
        tool_call_id: str,
        status: ToolExecutionStatus,
        result_data: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        postcondition_verified: Optional[bool] = None,
        executed_at: Optional[datetime] = None,
    ) -> ToolCallRecord:
        """Update a previously-recorded tool call in place (status/result/postcondition).

        ``record_tool_call`` above is create-or-dedup (a retried call with the
        same idempotency_key gets back the existing row unchanged); this is
        the explicit update path the Action Agent uses to move a PENDING
        record it created at proposal time to its real SUCCESS/FAILED
        outcome once the tool actually runs.
        """
        record = self._session.execute(
            select(ToolCallRecord).where(ToolCallRecord.tool_call_id == tool_call_id)
        ).scalar_one_or_none()
        if record is None:
            raise ValueError(f"unknown tool_call_id: {tool_call_id!r}")
        record.status = status
        if result_data is not None:
            record.result_data = result_data
        if error is not None:
            record.error = error
        if postcondition_verified is not None:
            record.postcondition_verified = postcondition_verified
        if executed_at is not None:
            record.executed_at = executed_at
        self._session.commit()
        self._session.refresh(record)
        return record

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------

    def create_approval_record(
        self,
        approval_id: str,
        mission_id: str,
        step_id: str,
        action_summary: str,
        requested_by: str,
        tool_call_id: Optional[str] = None,
        payload_fingerprint: Optional[str] = None,
        approved_payload: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            approval_id=approval_id,
            mission_id=mission_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            action_summary=action_summary,
            requested_by=requested_by,
            status=ApprovalStatus.PENDING,
            payload_fingerprint=payload_fingerprint,
            approved_payload=approved_payload,
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return record

    def update_approval_record(
        self,
        approval_id: str,
        status: ApprovalStatus,
        decision_by: Optional[str] = None,
        decision_at: Optional[datetime] = None,
        decision_reason: Optional[str] = None,
    ) -> ApprovalRecord:
        """Resolve (or re-file) an approval decision.

        A resolved status (approved/rejected/edit_required) must record who
        decided and when -- CLAUDE.md is explicit that a pending approval
        must never silently expire into an auto-approve, so this method
        never lets a resolved status through without both fields.
        """
        record = self._session.get(ApprovalRecord, approval_id)
        if record is None:
            raise ValueError(f"unknown approval_id: {approval_id!r}")
        if record.status == ApprovalStatus.STALE:
            raise ValueError(
                f"approval {approval_id!r} is stale; a stale approval is terminal and can never be "
                "re-decided or reactivated -- a new approval request is required"
            )
        if status == ApprovalStatus.STALE:
            raise ValueError("use invalidate_approval_record() to mark an approval stale")
        if status in _RESOLVED_APPROVAL_STATUSES and (decision_by is None or decision_at is None):
            raise ValueError(
                "a resolved approval (approved/rejected/edit_required) must record "
                "decision_by and decision_at"
            )
        record.status = status
        record.decision_by = decision_by
        record.decision_at = decision_at
        record.decision_reason = decision_reason
        self._session.commit()
        self._session.refresh(record)
        return record

    def invalidate_approval_record(
        self,
        approval_id: str,
        *,
        invalidated_at: datetime,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRecord:
        """Mark an APPROVED approval STALE (Phase 11).

        Unlike ``update_approval_record`` this never touches ``decision_by``/
        ``decision_at``/``decision_reason`` -- who approved and when stays on the
        record; why it stopped being valid goes in the ``invalidation_*`` fields.
        STALE is terminal: ``update_approval_record`` refuses to change it.
        """
        record = self._session.get(ApprovalRecord, approval_id)
        if record is None:
            raise ValueError(f"unknown approval_id: {approval_id!r}")
        if record.status != ApprovalStatus.APPROVED:
            raise ValueError(
                f"only an APPROVED approval can become stale; {approval_id!r} is {record.status.value}"
            )
        record.status = ApprovalStatus.STALE
        record.invalidated_at = invalidated_at
        record.invalidation_reason = reason
        record.invalidation_details = details or {}
        self._session.commit()
        self._session.refresh(record)
        return record

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def append_audit_event(
        self,
        event_id: str,
        mission_id: str,
        event_type: str,
        actor: str,
        message: str,
        step_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuditLog:
        """Append an immutable audit trail entry. There is no update/delete --
        the audit trail is append-only by design."""
        entry = AuditLog(
            event_id=event_id,
            mission_id=mission_id,
            step_id=step_id,
            event_type=event_type,
            actor=actor,
            message=message,
            event_metadata=metadata or {},
        )
        self._session.add(entry)
        self._session.commit()
        self._session.refresh(entry)
        return entry

    def list_audit_events(self, mission_id: str) -> List[AuditLog]:
        """Every audit entry for a mission, in insertion (chronological) order."""
        stmt = select(AuditLog).where(AuditLog.mission_id == mission_id).order_by(AuditLog.id)
        return list(self._session.execute(stmt).scalars().all())

    def get_latest_plan_snapshot(self, mission_id: str) -> Optional[MissionPlan]:
        """The most recently audited MissionPlan for a mission, or None if never planned.

        The plan itself has no dedicated table -- Phase 5's Orchestrator
        (app/graph/orchestrator.py) snapshots the full validated plan into an
        audit event (``event_type="plan_generated"``) at planning time, and
        this reconstructs it from there. This is what makes cross-process
        mission resumption possible without introducing a second, competing
        store of truth for the plan alongside the Context Service.
        """
        stmt = (
            select(AuditLog)
            .where(AuditLog.mission_id == mission_id, AuditLog.event_type == "plan_generated")
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        entry = self._session.execute(stmt).scalar_one_or_none()
        if entry is None or "plan" not in entry.event_metadata:
            return None
        return MissionPlan.model_validate(entry.event_metadata["plan"])

    # ------------------------------------------------------------------
    # Phase 13: action candidates + the student's target selections
    # ------------------------------------------------------------------

    def upsert_action_candidate(
        self,
        *,
        mission_id: str,
        tool_name: str,
        resource_type: str,
        resource_id: int,
        title: str,
        status: str,
        assessment: Dict[str, Any],
        assessed_at: datetime,
        recommendation: Optional[Dict[str, Any]] = None,
        recommended_by_step_id: Optional[str] = None,
        sequence: Optional[int] = None,
    ) -> ActionCandidateRecord:
        """Insert a candidate, or update its title/assessment in place (one row
        per mission/tool/resource). The recommendation is kept from the first
        insert unless a new one is given."""
        record = self.get_action_candidate(mission_id, tool_name, resource_type, resource_id)
        if record is None:
            record = ActionCandidateRecord(
                mission_id=mission_id, tool_name=tool_name, resource_type=resource_type, resource_id=resource_id,
                sequence=sequence or 0,
            )
            self._session.add(record)
        record.title = title
        record.status = status
        record.assessment = assessment
        record.assessed_at = assessed_at
        if recommendation is not None:
            record.recommendation = recommendation
        if recommended_by_step_id is not None:
            record.recommended_by_step_id = recommended_by_step_id
        self._session.commit()
        self._session.refresh(record)
        return record

    def get_action_candidate(
        self, mission_id: str, tool_name: str, resource_type: str, resource_id: int
    ) -> Optional[ActionCandidateRecord]:
        stmt = select(ActionCandidateRecord).where(
            ActionCandidateRecord.mission_id == mission_id,
            ActionCandidateRecord.tool_name == tool_name,
            ActionCandidateRecord.resource_type == resource_type,
            ActionCandidateRecord.resource_id == resource_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_action_candidates(self, mission_id: str) -> List[ActionCandidateRecord]:
        stmt = (
            select(ActionCandidateRecord)
            .where(ActionCandidateRecord.mission_id == mission_id)
            .order_by(ActionCandidateRecord.sequence, ActionCandidateRecord.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    def record_target_selection(
        self,
        *,
        selection_id: str,
        mission_id: str,
        step_id: str,
        tool_name: str,
        resource_type: str,
        resource_id: int,
        title: str,
        selected_by: str,
    ) -> TargetSelectionRecord:
        record = TargetSelectionRecord(
            selection_id=selection_id, mission_id=mission_id, step_id=step_id, tool_name=tool_name,
            resource_type=resource_type, resource_id=resource_id, title=title, selected_by=selected_by,
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return record

    def supersede_target_selection(self, selection_id: str, *, reason: str) -> TargetSelectionRecord:
        record = self._session.get(TargetSelectionRecord, selection_id)
        if record is None:
            raise ValueError(f"unknown selection_id: {selection_id!r}")
        if record.superseded_at is None:
            record.superseded_at = utc_now()
            record.superseded_reason = reason
            self._session.commit()
            self._session.refresh(record)
        return record

    def list_active_target_selections(self, mission_id: str) -> Dict[str, SelectedTarget]:
        """The mission's current (not superseded) selections, keyed by step id."""
        stmt = (
            select(TargetSelectionRecord)
            .where(TargetSelectionRecord.mission_id == mission_id, TargetSelectionRecord.superseded_at.is_(None))
            .order_by(TargetSelectionRecord.selected_at)
        )
        return {
            record.step_id: SelectedTarget(
                selection_id=record.selection_id, mission_id=record.mission_id, step_id=record.step_id,
                tool_name=record.tool_name, resource_type=record.resource_type, resource_id=record.resource_id,
                title=record.title, selected_by=record.selected_by, selected_at=record.selected_at,
            )
            for record in self._session.execute(stmt).scalars().all()
        }

    def get_active_target_selection(self, mission_id: str, step_id: str) -> Optional[SelectedTarget]:
        return self.list_active_target_selections(mission_id).get(step_id)

    # ------------------------------------------------------------------
    # Agent runs
    # ------------------------------------------------------------------

    def list_agent_runs(self, mission_id: str) -> List[AgentRun]:
        """Every recorded agent execution for a mission, in execution order."""
        stmt = select(AgentRun).where(AgentRun.mission_id == mission_id).order_by(AgentRun.id)
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def store_memory(
        self,
        memory_id: str,
        category: str,
        content: str,
        student_id: Optional[str] = None,
        mission_id: Optional[str] = None,
    ) -> MemoryRecord:
        memory = MemoryRecord(
            memory_id=memory_id,
            student_id=student_id,
            mission_id=mission_id,
            category=category,
            content=content,
        )
        self._session.add(memory)
        self._session.commit()
        self._session.refresh(memory)
        return memory

    def get_relevant_memories(
        self,
        student_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> List[MemoryRecord]:
        """Most recent memories matching the given filters.

        "Relevant" here means filtered-and-recent, not similarity-ranked --
        semantic recall is the Knowledge/RAG Agent's job in a later phase.
        """
        stmt = select(MemoryRecord)
        if student_id is not None:
            stmt = stmt.where(MemoryRecord.student_id == student_id)
        if mission_id is not None:
            stmt = stmt.where(MemoryRecord.mission_id == mission_id)
        if category is not None:
            stmt = stmt.where(MemoryRecord.category == category)
        stmt = stmt.order_by(MemoryRecord.created_at.desc()).limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # Compact student context for future agents
    # ------------------------------------------------------------------

    def get_student_context(self, student_id: str) -> StudentContext:
        """Build a compact, typed snapshot of a student's DB-backed state.

        ``student_id`` is the student's natural key (``student_code``).
        """
        student = self._session.execute(
            select(Student).where(Student.student_code == student_id)
        ).scalar_one_or_none()
        if student is None:
            raise ValueError(f"unknown student_id: {student_id!r}")

        enrollments = list(
            self._session.execute(
                select(Enrollment).where(Enrollment.student_id == student.id)
            ).scalars()
        )
        course_codes = [e.course.code for e in enrollments]

        attendance_rows = list(
            self._session.execute(
                select(AttendanceRecord)
                .join(Enrollment, AttendanceRecord.enrollment_id == Enrollment.id)
                .where(Enrollment.student_id == student.id)
            ).scalars()
        )
        attendance = [
            {
                "course_code": row.enrollment.course.code,
                "classes_attended": row.classes_attended,
                "classes_conducted": row.classes_conducted,
            }
            for row in attendance_rows
        ]

        open_cases = list(
            self._session.execute(
                select(CampusCase).where(
                    CampusCase.student_id == student.id,
                    CampusCase.status.in_([CaseStatus.OPEN, CaseStatus.IN_PROGRESS]),
                )
            ).scalars()
        )

        now = utc_now()
        upcoming_events = list(
            self._session.execute(
                select(Event)
                .join(EventRegistration, EventRegistration.event_id == Event.id)
                .where(EventRegistration.student_id == student.id, Event.start_at >= now)
                .order_by(Event.start_at)
            ).scalars()
        )

        application_count = len(
            list(
                self._session.execute(
                    select(Application).where(Application.student_id == student.id)
                ).scalars()
            )
        )

        return StudentContext(
            student_code=student.student_code,
            full_name=student.user.full_name,
            department_code=student.department.code,
            year=student.year,
            semester=student.semester,
            cgpa=student.cgpa,
            interests=student.interests,
            career_goal=student.career_goal,
            course_codes=course_codes,
            attendance=attendance,
            open_case_codes=[c.case_code for c in open_cases],
            upcoming_event_titles=[e.title for e in upcoming_events],
            application_count=application_count,
        )
