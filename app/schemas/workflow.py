"""Phase 16: workflow (permission/leave/OD) request views and the Permission Agent's intent."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

REQUEST_TYPE_LABELS = {
    "event_permission": "Event Permission",
    "attendance_permission": "Attendance Permission",
    "leave_request": "Leave Request",
    "od_request": "On-Duty (OD) Request",
}


class RequestEvent(BaseModel):
    event_id: int
    title: str
    starts_at: datetime
    ends_at: datetime
    location: str


class AffectedClass(BaseModel):
    course_code: str
    course_title: str
    section: str
    starts_at: datetime
    ends_at: datetime
    start_local: str
    end_local: str
    room: str
    faculty_name: str
    session_status: str
    # The student's mark in that session, when one exists (attendance permission).
    my_mark: Optional[str] = None
    attendance_percentage: Optional[float] = None
    required_percentage: Optional[float] = None
    standing: str = "unknown"


class RequestContext(BaseModel):
    """Everything collected deterministically for the reviewer; stored as JSON."""

    event: Optional[RequestEvent] = None
    request_date: Optional[date] = None
    day_part: Optional[str] = None  # full_day / morning / afternoon
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    affected_classes: List[AffectedClass] = Field(default_factory=list)
    timetable_conflict: bool = False
    student_name: Optional[str] = None
    student_year: Optional[int] = None
    student_section: Optional[str] = None
    department_code: Optional[str] = None
    notes: List[str] = Field(default_factory=list)


class WorkflowRequestView(BaseModel):
    request_id: str
    request_type: str
    type_label: str
    status: str
    title: str
    reason: str
    student_id: Optional[str] = None
    student_name: Optional[str] = None
    reviewer_name: Optional[str] = None
    routing_basis: str
    routing_note: str
    context: RequestContext
    created_at: datetime
    submitted_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    decision_reason: Optional[str] = None


class PrepareRequestBody(BaseModel):
    message: str = Field(min_length=3, max_length=1000)


class SubmitRequestBody(BaseModel):
    request_id: str = Field(min_length=4, max_length=20)
    reason: Optional[str] = Field(default=None, min_length=3, max_length=1000)


class DecisionBody(BaseModel):
    comment: Optional[str] = Field(default=None, max_length=1000)


class PermissionPreview(BaseModel):
    """The Permission Agent's reply: a draft to confirm, or what it still needs."""

    outcome: Literal["draft_ready", "needs_clarification", "not_supported"]
    message: str
    request: Optional[WorkflowRequestView] = None
    options: List[str] = Field(default_factory=list)
    interpretation: dict = Field(default_factory=dict)
    live_ai: bool = False


# ---------------------------------------------------------------------------
# Permission Agent planning (LLM structured output)
# ---------------------------------------------------------------------------


class PermissionIntent(BaseModel):
    request_type: Literal["event_permission", "attendance_permission", "leave_request", "od_request", "unclear"] = Field(
        description=(
            "event_permission: permission to attend/participate in a campus event (contest, workshop, fest). "
            "attendance_permission: the student already missed a class and asks for it to be considered/excused. "
            "leave_request: the student will be away (sick, personal, travel) and asks for leave. "
            "od_request: on-duty -- away on official college work (representing the college, placement drive). "
            "unclear: none of these, or not a request."
        )
    )
    event_reference: Optional[str] = Field(
        default=None, max_length=120,
        description="The exact words of the message that name the event, if any. Never invent an event.",
    )
    date_reference: Literal["today", "tomorrow", "yesterday", "specific_date", "none"] = Field(
        default="none", description="Which day the request is about, as the student said it.",
    )
    specific_date: Optional[date] = Field(default=None, description="Only when date_reference is specific_date.")
    day_part: Literal["full_day", "morning", "afternoon"] = Field(
        default="full_day", description="Part of the day, when the student says morning or afternoon.",
    )
    reason: Optional[str] = Field(
        default=None, max_length=500, description="The reason the student gave, in their words. Null if none was given.",
    )
