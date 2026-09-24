"""The signed-in student's own workspace data (Phase 15): ``/me/*``.

Every endpoint takes the student from the JWT (``current_student``); none
accepts a student id from the client.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth_deps import current_student
from app.api.deps import get_knowledge_service, get_now, get_session
from app.schemas.faculty import LiveClassStatus
from app.schemas.student_portal import (
    CourseAttendance,
    DashboardSummary,
    ExamItem,
    NotificationItem,
    RequestItem,
    StudentProfile,
    TodaySchedule,
)
from app.services import class_schedule, student_portal
from app.services.knowledge import KnowledgeService

router = APIRouter(prefix="/me", tags=["student portal"])


@router.get("/profile", response_model=StudentProfile)
def get_profile(student_id: str = Depends(current_student), session: Session = Depends(get_session)) -> StudentProfile:
    result = student_portal.profile(session, student_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Your student record was not found.")
    return result


@router.get("/dashboard", response_model=DashboardSummary)
def get_dashboard(
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service),
) -> DashboardSummary:
    result = student_portal.dashboard(session, knowledge, student_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Your student record was not found.")
    return result


@router.get("/attendance", response_model=List[CourseAttendance])
def get_attendance(
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service),
) -> List[CourseAttendance]:
    return student_portal.attendance(session, knowledge, student_id)


@router.get("/attendance/{course_code}", response_model=CourseAttendance)
def get_course_attendance(
    course_code: str,
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service),
) -> CourseAttendance:
    match = next(
        (c for c in student_portal.attendance(session, knowledge, student_id) if c.course_code.upper() == course_code.upper()),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail=f"You are not enrolled in {course_code}.")
    return match


@router.get("/timetable/today", response_model=TodaySchedule)
def get_today(student_id: str = Depends(current_student), session: Session = Depends(get_session)) -> TodaySchedule:
    return student_portal.today_schedule(session, student_id)


@router.get("/live-class", response_model=LiveClassStatus)
def get_live_class(
    student_id: str = Depends(current_student), session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> LiveClassStatus:
    """Phase 16: the student's current class from real attendance-session data."""
    return class_schedule.get_current_class(session, student_id, now)


@router.get("/exams", response_model=List[ExamItem])
def get_exams(
    student_id: str = Depends(current_student),
    session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service),
) -> List[ExamItem]:
    return student_portal.exams(session, knowledge, student_id)


@router.get("/requests", response_model=List[RequestItem])
def get_requests(student_id: str = Depends(current_student), session: Session = Depends(get_session)) -> List[RequestItem]:
    return student_portal.requests(session, student_id)


@router.get("/notifications", response_model=List[NotificationItem])
def get_notifications(student_id: str = Depends(current_student), session: Session = Depends(get_session)) -> List[NotificationItem]:
    return student_portal.notifications(session, student_id)
