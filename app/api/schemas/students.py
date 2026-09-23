"""Response models for the student-facing endpoints (§2, §5)."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class StudentProfileView(BaseModel):
    student_code: str
    full_name: str
    department_code: str
    year: int
    semester: int
    cgpa: float


class AttendanceOverviewItem(BaseModel):
    course_code: str
    course_title: str
    classes_attended: int
    classes_conducted: int
    current_percentage: Optional[float] = None
    required_percentage: Optional[float] = None
    eligible_now: Optional[bool] = None
    threshold_status: str  # PolicyThreshold.status.value -- "ok"/"not_found"/"ambiguous", honest about missing grounding


class UpcomingExamItem(BaseModel):
    course_code: str
    course_title: str
    exam_type: str
    scheduled_start: str
    location: str


class RecommendedOpportunityItem(BaseModel):
    title: str
    company: str
    status: str  # OpportunityEligibilityStatus value
    minimum_cgpa: float
    blocking_reasons: List[str] = []


class RelevantEventItem(BaseModel):
    event_id: int
    title: str
    category: str
    start_at: datetime
    availability: str
    already_registered: bool


class GrievanceItem(BaseModel):
    case_code: str
    category: str
    status: str
    priority: str
    department: str
    response_breached: Optional[bool] = None
    resolution_breached: Optional[bool] = None


class CalendarEntryView(BaseModel):
    id: int
    title: str
    start_at: datetime
    end_at: datetime
    source_type: str
    source_id: Optional[int] = None


class DashboardResponse(BaseModel):
    student: StudentProfileView
    attendance: List[AttendanceOverviewItem]
    upcoming_exams: List[UpcomingExamItem]
    recommended_opportunities: List[RecommendedOpportunityItem]
    relevant_events: List[RelevantEventItem]
    grievances: List[GrievanceItem]
    calendar: List[CalendarEntryView]
