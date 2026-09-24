"""Phase 13: selectable action candidates and the student's explicit selection.

An agent recommends candidates. Only the student selects one, and only after
the server has re-validated it against current data. These models carry just
what the UI and the deterministic continuation need -- never a copy of the
full Event row. ``resource_id`` is always the real database id.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.common import NonBlankStr
from app.schemas.events import ExamConflict, TimetableConflict

# The only action whose target can currently be chosen from a candidate list.
SELECTABLE_ACTIONS = ("register_event",)
ACTION_RESOURCE_TYPES = {"register_event": "event"}


class CandidateStatus(str, Enum):
    """Deterministic selectability of one candidate (app/rules/candidate_status.py).

    Only ELIGIBLE may be selected. Every other state is still shown, with its
    reasons, so the student can see why a recommendation is not actionable.
    """

    ELIGIBLE = "eligible"
    CONFLICT = "conflict"
    FULL = "full"
    DEADLINE_PASSED = "deadline_passed"
    ALREADY_REGISTERED = "already_registered"
    UNAVAILABLE = "unavailable"
    NEEDS_REVIEW = "needs_review"


class CandidateAssessment(BaseModel):
    """The deterministic, current-data verdict for one candidate."""

    status: CandidateStatus
    reasons: List[str] = Field(default_factory=list)
    schedule_checked: bool = False
    # Where the schedule data came from: the mission's verified Academic task
    # results (at discovery) or the student's current timetable/exams (refresh
    # and selection).
    schedule_source: Optional[str] = None
    timetable_conflicts: List[TimetableConflict] = Field(default_factory=list)
    exam_conflicts: List[ExamConflict] = Field(default_factory=list)
    confirmed_registrations: Optional[int] = None
    assessed_at: datetime

    @property
    def selectable(self) -> bool:
        return self.status == CandidateStatus.ELIGIBLE


class ActionCandidate(BaseModel):
    """One candidate the student may pick as an action's target."""

    mission_id: NonBlankStr
    tool_name: Literal["register_event"]
    resource_type: Literal["event"]
    resource_id: int = Field(gt=0)
    title: NonBlankStr
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    venue: Optional[str] = None
    registration_deadline: Optional[datetime] = None
    capacity: Optional[int] = None
    seats_remaining: Optional[int] = None
    # Why an agent recommended it -- a recommendation is not a selection.
    recommended_by_step_id: Optional[str] = None
    recommendation_reason: str = ""
    matched_terms: List[str] = Field(default_factory=list)
    matched_skill_gaps: List[str] = Field(default_factory=list)
    evidence_refs: List[str] = Field(default_factory=list)
    assessment: CandidateAssessment
    selectable: bool = False
    selected: bool = False


class SelectedTarget(BaseModel):
    """A server-validated, persisted selection: the only USER_SELECTION authorization."""

    selection_id: NonBlankStr
    mission_id: NonBlankStr
    step_id: NonBlankStr
    tool_name: Literal["register_event"]
    resource_type: Literal["event"]
    resource_id: int = Field(gt=0)
    title: NonBlankStr
    selected_by: NonBlankStr
    selected_at: datetime


class SelectionResult(str, Enum):
    SELECTED = "selected"
    ALREADY_SELECTED = "already_selected"
    BLOCKED = "blocked"


class SelectionOutcome(BaseModel):
    """What a selection request did. BLOCKED never creates a proposal or approval."""

    result: SelectionResult
    message: str
    candidate: Optional[ActionCandidate] = None
    selection: Optional[SelectedTarget] = None
    superseded_selection_id: Optional[str] = None
    superseded_approval_id: Optional[str] = None
