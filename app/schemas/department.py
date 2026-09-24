"""Phase 17: HOD / department views and the HOD query plan (LLM structured output)."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.faculty import MarkTallyView


class HodProfileView(BaseModel):
    faculty_id: int
    employee_code: str
    full_name: str
    designation: str
    email: str
    department_id: int
    department_code: str
    department_name: str


class DepartmentClassView(BaseModel):
    """One class meeting in the department today, with its deterministic state."""

    session_id: Optional[int] = None
    assignment_id: int
    course_code: str
    course_title: str
    class_label: str  # e.g. "CSE 3-1"
    year: int
    section: str
    faculty_id: int
    faculty_name: str
    room: str
    scheduled_start: datetime
    scheduled_end: datetime
    start_local: str
    end_local: str
    session_status: str  # scheduled / active / closed / cancelled
    state: str  # upcoming / due / delayed / not_held / active / completed / cancelled
    is_extra_class: bool = False
    actual_started_at: Optional[datetime] = None
    actual_closed_at: Optional[datetime] = None
    tally: Optional[MarkTallyView] = None


class DepartmentDashboard(BaseModel):
    profile: HodProfileView
    date: date
    now_local: str
    start_grace_minutes: int
    faculty_count: int
    student_count: int
    classes_today: int
    active_classes: int
    completed_classes: int
    not_started_classes: int
    pending_faculty_requests: int
    escalated_student_requests: int
    activity: List[DepartmentClassView]


class FacultySummary(BaseModel):
    faculty_id: int
    employee_code: str
    full_name: str
    designation: str
    email: str
    is_hod: bool = False
    courses: List[str] = Field(default_factory=list)
    classes_today: int = 0
    active_class: Optional[str] = None
    not_started_today: int = 0
    pending_requests_to_review: int = 0
    own_open_requests: int = 0


class StudentRisk(BaseModel):
    student_id: str
    full_name: str
    year: int
    semester: int
    section: Optional[str] = None
    overall_percentage: Optional[float] = None
    classes_attended: int = 0
    classes_conducted: int = 0
    courses_below_threshold: List[str] = Field(default_factory=list)
    open_complaints: int = 0


class CourseAttendanceSummary(BaseModel):
    assignment_id: int
    course_code: str
    course_title: str
    class_label: str
    faculty_name: str
    students: int
    classes_attended: int
    classes_conducted: int
    percentage: Optional[float] = None
    below_threshold: int = 0


class SectionAttendanceSummary(BaseModel):
    class_label: str
    year: int
    section: str
    students: int
    classes_attended: int
    classes_conducted: int
    percentage: Optional[float] = None
    below_threshold: int = 0


class LowAttendanceEntry(BaseModel):
    student_id: str
    full_name: str
    class_label: str
    course_code: str
    course_title: str
    classes_attended: int
    classes_conducted: int
    percentage: float


class SessionSummary(BaseModel):
    session_id: int
    course_code: str
    course_title: str
    class_label: str
    faculty_name: str
    session_date: date
    start_local: str
    status: str
    actual_started_at: Optional[datetime] = None
    tally: MarkTallyView


class AttendanceInsights(BaseModel):
    required_percentage: Optional[float] = None
    sections: List[SectionAttendanceSummary]
    courses: List[CourseAttendanceSummary]
    low_attendance: List[LowAttendanceEntry]
    recent_sessions: List[SessionSummary]
    incomplete_sessions: List[SessionSummary]
    not_held_today: List[DepartmentClassView]


class DepartmentComplaint(BaseModel):
    case_code: str
    student_id: str
    student_name: str
    category: str
    priority: str
    status: str
    office: str
    created_at: datetime
    response_due_at: Optional[datetime] = None
    resolution_due_at: Optional[datetime] = None
    response_breached: bool = False
    resolution_breached: bool = False


class DepartmentOverview(BaseModel):
    """The Department page: visibility only, no editing."""

    dashboard: DepartmentDashboard
    faculty: List[FacultySummary]
    at_risk_students: int
    below_threshold_entries: int
    required_percentage: Optional[float] = None


class StaffNotificationItem(BaseModel):
    id: int
    title: str
    body: str
    category: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# HOD agent planning (LLM structured output)
# ---------------------------------------------------------------------------

HodIntent = Literal[
    "classes_running", "classes_today", "faculty_teaching_today", "classes_not_started", "below_threshold",
    "lowest_course_attendance", "section_present_today", "pending_faculty_requests", "escalated_student_requests",
    "complaints_breached", "department_events", "department_overview", "make_request", "unknown",
]


class HodQueryPlan(BaseModel):
    intent: HodIntent = Field(
        description=(
            "classes_running: which/how many classes are in session right now. "
            "classes_today: the department's classes today. "
            "faculty_teaching_today: which faculty members teach today. "
            "classes_not_started: classes that have not started / did not start on time / are delayed. "
            "below_threshold: students below the required attendance percentage. "
            "lowest_course_attendance: which course or section has the lowest attendance. "
            "section_present_today: how many students were present in a year/section today. "
            "pending_faculty_requests: faculty leave/substitution/OD/permission requests waiting for the HOD. "
            "escalated_student_requests: student requests escalated to the HOD. "
            "complaints_breached: department students' complaints that breached their SLA. "
            "department_events: upcoming campus events relevant to the department. "
            "department_overview: whether things are running normally in the department today (a broad check). "
            "make_request: the HOD wants to ASK for something themselves. "
            "unknown: anything else."
        )
    )
    year_reference: Optional[int] = Field(default=None, ge=1, le=6, description="Year of study named in the question, if any.")
    section_reference: Optional[str] = Field(default=None, max_length=10, description="Section named in the question, if any.")
