"""Events & Opportunity Agent vertical-slice schemas (Phase 6).

Mirrors app/schemas/academic.py's structure. Timetable/exam conflict
checking reuses ``app.schemas.academic.TimetableEntry``/``ExamEntry`` for
whatever upstream Academic Agent facts the Orchestrator's dispatcher merges
in (app/graph/dispatcher.py) when this agent's task depends on one --
CLAUDE.md's "communicate only through... their Pydantic schemas" applies to
consuming another agent's schema, not just producing your own.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import NonBlankStr
from app.schemas.enums import VerificationStatus
from app.schemas.evidence import Evidence

# ---------------------------------------------------------------------------
# Events intent (LLM classification boundary)
# ---------------------------------------------------------------------------


class EventsIntent(str, Enum):
    """The supported categories of events/opportunity request the LLM may classify a query into."""

    EVENT_DISCOVERY = "event_discovery"
    REGISTRATION_STATUS = "registration_status"
    POLICY_QUESTION = "policy_question"
    UNKNOWN = "unknown"


class EventsIntentResult(BaseModel):
    """Structured output of ``LLMProvider.classify_events_intent``."""

    intent: EventsIntent


# ---------------------------------------------------------------------------
# Service DTOs (app/services/events.py)
# ---------------------------------------------------------------------------


class EventSummary(BaseModel):
    event_id: int
    title: NonBlankStr
    description: NonBlankStr
    category: NonBlankStr
    organizer: NonBlankStr
    location: NonBlankStr
    start_at: datetime
    end_at: datetime
    registration_deadline: Optional[datetime] = None
    capacity: Optional[int] = None
    status: NonBlankStr


# ---------------------------------------------------------------------------
# Deterministic availability + conflicts (app/rules/event_availability.py)
# ---------------------------------------------------------------------------


class EventAvailabilityStatus(str, Enum):
    """Whether an event can currently be recommended as registerable.

    An event must never be reported AVAILABLE if it is full, closed, or
    outside its registration window -- it may still be *discovered* (shown
    to the student) in any of these states, just not recommended as
    something they can act on right now.
    """

    AVAILABLE = "available"
    FULL = "full"
    REGISTRATION_CLOSED = "registration_closed"
    NOT_OPEN = "not_open"


class TimetableConflict(BaseModel):
    course_code: NonBlankStr
    weekday: int = Field(ge=0, le=6)
    start_time: str
    end_time: str


class ExamConflict(BaseModel):
    course_code: NonBlankStr
    exam_type: NonBlankStr
    scheduled_start: datetime
    scheduled_end: datetime


class EventAssessment(BaseModel):
    """One event, deterministically assessed: availability + conflicts + registration status.

    Conflicted events are still returned here -- never hidden -- but
    ``timetable_conflicts``/``exam_conflicts`` being non-empty is what lets
    the response clearly distinguish them from conflict-free events.
    """

    event: EventSummary
    availability: EventAvailabilityStatus
    already_registered: bool = False
    registration_status: Optional[str] = None
    timetable_conflicts: List[TimetableConflict] = Field(default_factory=list)
    exam_conflicts: List[ExamConflict] = Field(default_factory=list)
    conflict_check_performed: bool = False


# ---------------------------------------------------------------------------
# Final response generation (app/llm/base.py)
# ---------------------------------------------------------------------------


class EventsResponseContext(BaseModel):
    intent: EventsIntent
    verification_status: VerificationStatus
    verification_issues: List[str] = Field(default_factory=list)
    student_name: Optional[str] = None
    assessments: List[EventAssessment] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
