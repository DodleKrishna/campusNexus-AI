"""Dashboard composition (Phase 8 §5): calls only existing, already-approved
read services -- never reimplements academic/career/events/services logic.

The one piece of actual computation here (attendance percentage/eligibility)
reuses the exact same deterministic pipeline the Academic Agent uses
(``extract_attendance_threshold`` over real RAG evidence, then
``compute_attendance``) rather than a hardcoded/ungrounded percentage, per
CLAUDE.md's RAG-grounding rule -- a required-attendance figure is a policy
fact, not something to invent client-side.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import List

from sqlalchemy.orm import Session

from app.api.schemas.students import (
    AttendanceOverviewItem,
    CalendarEntryView,
    DashboardResponse,
    GrievanceItem,
    RecommendedOpportunityItem,
    RelevantEventItem,
    StudentProfileView,
    UpcomingExamItem,
)
from app.db.repositories.calendar import get_student_calendar
from app.rules.attendance import compute_attendance
from app.rules.event_availability import compute_availability
from app.rules.opportunity_eligibility import compute_eligibility
from app.rules.policy_threshold import extract_attendance_threshold
from app.rules.sla import compute_sla_breaches
from app.schemas.career import OpportunityEligibilityStatus
from app.services import academic as academic_service
from app.services import career as career_service
from app.services import events as events_service
from app.services import services as services_service
from app.services.knowledge import KnowledgeService

_MAX_RECOMMENDED_OPPORTUNITIES = 5
_MAX_RELEVANT_EVENTS = 5


def build_dashboard(session: Session, knowledge_service: KnowledgeService, student_id: str) -> DashboardResponse:
    now = datetime.now(timezone.utc)
    today = date.today()

    profile = academic_service.get_student_academic_profile(session, student_id)
    student = StudentProfileView(
        student_code=profile.student_code, full_name=profile.full_name, department_code=profile.department_code,
        year=profile.year, semester=profile.semester, cgpa=profile.cgpa,
    )

    threshold_evidence = knowledge_service.get_active_policy("attendance_policy", as_of=today, visibility="public")
    threshold = extract_attendance_threshold(threshold_evidence)
    attendance: List[AttendanceOverviewItem] = []
    for snapshot in academic_service.get_attendance(session, student_id):
        item = AttendanceOverviewItem(
            course_code=snapshot.course_code, course_title=snapshot.course_title,
            classes_attended=snapshot.classes_attended, classes_conducted=snapshot.classes_conducted,
            threshold_status=threshold.status.value,
        )
        if threshold.required_percentage is not None:
            calc = compute_attendance(snapshot.classes_attended, snapshot.classes_conducted, threshold.required_percentage)
            item.current_percentage = float(calc.current_percentage) if calc.current_percentage is not None else None
            item.required_percentage = float(threshold.required_percentage)
            item.eligible_now = calc.eligible_now
        attendance.append(item)

    upcoming_exams = [
        UpcomingExamItem(
            course_code=e.course_code, course_title=e.course_title, exam_type=e.exam_type,
            scheduled_start=e.scheduled_start, location=e.location,
        )
        for e in academic_service.get_exam_schedule(session, student_id)
        if datetime.fromisoformat(e.scheduled_start) >= now
    ]

    career_profile = career_service.get_career_profile(session, student_id)
    recommended: List[RecommendedOpportunityItem] = []
    if career_profile is not None:
        applications = {a.opportunity_title: a for a in career_service.get_applications(session, student_id)}
        for opportunity in career_service.get_open_opportunities(session):
            existing = applications.get(opportunity.title)
            eligibility = compute_eligibility(
                career_profile, opportunity, now=now,
                already_applied=existing is not None, application_status=existing.status if existing else None,
            )
            if eligibility.status == OpportunityEligibilityStatus.ELIGIBLE:
                recommended.append(
                    RecommendedOpportunityItem(
                        title=opportunity.title, company=opportunity.company, status=eligibility.status.value,
                        minimum_cgpa=opportunity.minimum_cgpa, blocking_reasons=eligibility.blocking_reasons,
                    )
                )
            if len(recommended) >= _MAX_RECOMMENDED_OPPORTUNITIES:
                break

    relevant_events: List[RelevantEventItem] = []
    for event in events_service.get_upcoming_events(session, now)[: _MAX_RELEVANT_EVENTS]:
        confirmed = events_service.get_registration_count(session, event.event_id)
        availability = compute_availability(event, now=now, confirmed_registrations=confirmed)
        already_registered = events_service.get_student_registration_status(session, student_id, event.event_id) is not None
        relevant_events.append(
            RelevantEventItem(
                event_id=event.event_id, title=event.title, category=event.category, start_at=event.start_at,
                availability=availability.value, already_registered=already_registered,
            )
        )

    grievances: List[GrievanceItem] = []
    for case in services_service.get_student_case_summaries(session, student_id):
        sla = services_service.get_case_sla_record(session, case.case_code)
        response_breached = resolution_breached = None
        if sla is not None:
            response_breached, resolution_breached = compute_sla_breaches(
                response_due_at=sla.response_due_at, resolution_due_at=sla.resolution_due_at,
                responded_at=sla.responded_at, resolved_at=sla.resolved_at, now=now,
            )
        grievances.append(
            GrievanceItem(
                case_code=case.case_code, category=case.category, status=case.status, priority=case.priority,
                department=case.department, response_breached=response_breached, resolution_breached=resolution_breached,
            )
        )

    calendar = [
        CalendarEntryView(id=entry.id, title=entry.title, start_at=entry.start_at, end_at=entry.end_at, source_type=entry.source_type, source_id=entry.source_id)
        for entry in get_student_calendar(session, student_id)
    ]

    return DashboardResponse(
        student=student, attendance=attendance, upcoming_exams=upcoming_exams,
        recommended_opportunities=recommended, relevant_events=relevant_events, grievances=grievances, calendar=calendar,
    )
