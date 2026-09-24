"""Phase 16: faculty workspace views, live class status and the faculty query plan."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

MarkStatus = Literal["present", "absent", "late", "excused"]


class WeeklySlotView(BaseModel):
    weekday: str
    start_time: str
    end_time: str
    room: str


class TeachingAssignmentView(BaseModel):
    assignment_id: int
    course_code: str
    course_title: str
    department_code: str
    year: int
    semester: int
    section: str
    academic_term: str
    roster_size: int
    weekly_slots: List[WeeklySlotView] = Field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.course_title} ({self.department_code} Year {self.year}, Section {self.section})"


class FacultyProfileView(BaseModel):
    faculty_id: int
    employee_code: str
    full_name: str
    department_code: str
    department_name: str
    designation: str
    email: str
    phone: Optional[str] = None
    assignments: List[TeachingAssignmentView] = Field(default_factory=list)


class MarkTallyView(BaseModel):
    roster: int
    present: int
    absent: int
    late: int
    excused: int
    unmarked: int


class FacultyClassView(BaseModel):
    """One class meeting as the faculty member sees it."""

    session_id: int
    assignment_id: int
    course_code: str
    course_title: str
    department_code: str
    year: int
    semester: int
    section: str
    session_date: date
    scheduled_start: datetime
    scheduled_end: datetime
    start_local: str
    end_local: str
    room: str
    status: str  # scheduled / active / closed / cancelled
    is_extra_class: bool
    actual_started_at: Optional[datetime] = None
    actual_closed_at: Optional[datetime] = None
    tally: MarkTallyView
    can_start: bool = False
    start_blocked_reason: Optional[str] = None


class RosterEntry(BaseModel):
    student_id: str
    full_name: str
    mark: Optional[MarkStatus] = None
    marked_at: Optional[datetime] = None
    classes_attended: Optional[int] = None
    classes_conducted: Optional[int] = None
    current_percentage: Optional[float] = None
    standing: str = "unknown"


class FacultyClassDetail(BaseModel):
    class_info: FacultyClassView
    roster: List[RosterEntry]
    required_percentage: Optional[float] = None


class FacultyDashboard(BaseModel):
    profile: FacultyProfileView
    date: date
    now_local: str
    classes_today: int
    active_class: Optional[FacultyClassView] = None
    students_across_today: int
    pending_requests: int
    today: List[FacultyClassView]


class AssignmentStanding(BaseModel):
    assignment: TeachingAssignmentView
    required_percentage: Optional[float] = None
    below_requirement: int = 0
    at_risk: int = 0
    students: List[RosterEntry]


class AttendanceMarkInput(BaseModel):
    student_id: str = Field(min_length=1, max_length=20)
    status: MarkStatus


class MarkAttendanceRequest(BaseModel):
    marks: List[AttendanceMarkInput] = Field(min_length=1, max_length=500)


class MarkAllRequest(BaseModel):
    status: MarkStatus = "present"


# ---------------------------------------------------------------------------
# Student live class status
# ---------------------------------------------------------------------------


class LiveClassInfo(BaseModel):
    session_id: Optional[int] = None
    course_code: str
    course_title: str
    section: str
    faculty_name: str
    room: str
    scheduled_start: datetime
    scheduled_end: datetime
    start_local: str
    end_local: str
    session_status: str  # scheduled / active / closed / cancelled
    is_extra_class: bool = False
    actual_started_at: Optional[datetime] = None
    actual_closed_at: Optional[datetime] = None


class LiveClassStatus(BaseModel):
    state: Literal["live", "scheduled", "closed", "cancelled", "no_class"]
    current: Optional[LiveClassInfo] = None
    my_attendance: Optional[str] = None  # present / absent / late / excused / not_marked
    my_attendance_marked_at: Optional[datetime] = None
    next_class: Optional[LiveClassInfo] = None
    message: str
    as_of: datetime
    timezone: str


# ---------------------------------------------------------------------------
# Faculty agent planning (LLM structured output)
# ---------------------------------------------------------------------------

FacultyIntent = Literal[
    "classes_today", "current_class", "next_class", "class_attendance", "absent_students",
    "below_threshold", "pending_requests", "make_request", "unknown",
]


class FacultyQueryPlan(BaseModel):
    intent: FacultyIntent = Field(
        description=(
            "classes_today: how many / which classes the faculty member teaches today. "
            "current_class: which class is happening now or whether it has started. "
            "next_class: the faculty member's next class today. "
            "class_attendance: how many students are present/absent/marked in a class session. "
            "absent_students: who is absent (or not yet marked) in a class session. "
            "below_threshold: which students are below the required attendance percentage in a class. "
            "pending_requests: student permission/leave requests waiting for this faculty member's decision. "
            "make_request: the faculty member wants to ASK for something themselves (leave, substitution, OD, permission). "
            "unknown: anything else."
        )
    )
    course_reference: Optional[str] = Field(
        default=None, max_length=80,
        description="The exact words of the question that name a course (code, title or abbreviation), if any. Never invent one.",
    )
    section_reference: Optional[str] = Field(
        default=None, max_length=10, description="The section named in the question (e.g. '1' for 'Section 1'), if any.",
    )
    year_reference: Optional[int] = Field(
        default=None, ge=1, le=6, description="The year of study named in the question (e.g. 3 for '3rd year'), if any.",
    )
