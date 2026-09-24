"""Response models for the authenticated student portal (Phase 15, ``/me/*``)."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class StudentProfile(BaseModel):
    student_code: str
    full_name: str
    first_name: str
    department_code: str
    department_name: str
    year: int
    semester: int
    cgpa: float


class PolicySource(BaseModel):
    document_id: str
    title: Optional[str] = None
    version: Optional[str] = None
    section: Optional[str] = None


class CourseAttendance(BaseModel):
    course_code: str
    course_title: str
    instructor: Optional[str] = None
    classes_attended: int
    classes_conducted: int
    current_percentage: Optional[float] = None
    required_percentage: Optional[float] = None
    eligible_now: Optional[bool] = None
    # good / at_risk / below_requirement / unknown (app/rules/attendance_standing.py)
    standing: str
    classes_needed_to_reach_threshold: Optional[int] = None
    maximum_additional_absences_allowed: Optional[int] = None
    threshold_reachable: Optional[bool] = None
    policy: Optional[PolicySource] = None


class OverallAttendance(BaseModel):
    """Total attended / total conducted across enrolled courses (a summary figure;
    eligibility is decided per course)."""

    percentage: float
    classes_attended: int
    classes_conducted: int


class ExamItem(BaseModel):
    course_code: str
    course_title: str
    exam_type: str
    starts_at: datetime
    ends_at: datetime
    venue: str
    # eligible / not_eligible / unknown -- attendance-based (app/rules/eligibility.py)
    eligibility: Optional[str] = None
    eligibility_caveat: Optional[str] = None


class ScheduleSlot(BaseModel):
    course_code: str
    course_title: str
    instructor: Optional[str] = None
    start_time: str
    end_time: str
    location: str
    status: str  # upcoming / now / completed


class TodaySchedule(BaseModel):
    date: date
    weekday: str
    timezone: str
    now_local: str
    slots: List[ScheduleSlot]


class RequestItem(BaseModel):
    """An action this student's missions asked for, and where its approval stands."""

    approval_id: str
    mission_id: str
    title: str
    status: str
    tool_name: Optional[str] = None
    requested_at: datetime
    decided_at: Optional[datetime] = None


class NotificationItem(BaseModel):
    id: int
    title: str
    body: str
    category: str
    status: str
    created_at: datetime


class DashboardSummary(BaseModel):
    profile: StudentProfile
    cgpa: float
    overall_attendance: Optional[OverallAttendance] = None
    courses_below_requirement: int = 0
    next_exam: Optional[ExamItem] = None
    pending_requests: int = 0
    unread_notifications: int = 0
