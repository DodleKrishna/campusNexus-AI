"""CAMPUS AI grounded answers: the minimum deterministic facts for one question, scoped to the caller.

Each builder reads only what its question needs, only for the server-resolved caller (the student or faculty
profile comes from the authenticated membership, never from the question), and returns:

* ``facts`` -- a few compact label/value pairs (never whole rows);
* ``draft`` -- a complete, correct answer rendered in code from those facts (no AI);
* ``must_mention`` -- names/titles a synthesized answer has to keep (e.g. every pending student);
* ``sources`` -- retrieved knowledge chunks the answer relies on (policy thresholds, procedures).

Official numbers (attendance percentages, exam eligibility, opportunity eligibility) come from the existing rule
modules; nothing here is computed by, or delegated to, an LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models.academic import AttendanceRecord, Course, Enrollment, Exam
from app.db.models.assignment import Assignment, AssignmentStatus
from app.db.models.exam import ExamAttendance, ExamAttendanceStatus, ExamTarget
from app.db.models.faculty import (
    AttendanceMarkStatus, AttendanceSession, AttendanceSessionStatus, SessionAttendanceMark, TeachingAssignment,
)
from app.db.models.identity import Student
from app.db.repositories.academics import get_exam_schedule as exam_rows_for_student
from app.rag.campus_knowledge import KnowledgeChunk
from app.rules.attendance import compute_attendance
from app.rules.eligibility import compute_exam_eligibility
from app.rules.opportunity_eligibility import compute_eligibility
from app.rules.policy_threshold import extract_attendance_threshold
from app.schemas.academic import AttendanceRuleStatus, ExamEligibilityStatus, ThresholdExtractionStatus
from app.schemas.enums import UserRole
from app.schemas.evidence import Evidence
from app.services import academic as academic_service
from app.services import assignments as assignment_service
from app.services import career as career_service
from app.services import events as events_service
from app.services import services as services_service
from app.services.class_schedule import local, planned_classes, roster, student_assignments

MAX_LIST = 6
KnowledgeLookup = Callable[[str, Sequence[str], int], List[KnowledgeChunk]]


@dataclass(frozen=True)
class GroundedIdentity:
    """The server-resolved caller. Built from the tool context / token, never from the question."""

    organization_id: int
    account_id: int
    role: UserRole
    display_name: Optional[str] = None
    student_code: Optional[str] = None
    faculty_profile_id: Optional[int] = None


@dataclass
class FactResult:
    facts: List[Tuple[str, Any]] = field(default_factory=list)
    draft: str = ""
    found: bool = False
    must_mention: List[str] = field(default_factory=list)
    sources: List[KnowledgeChunk] = field(default_factory=list)


def _when(value: datetime) -> str:
    return f"{local(value):%a %d %b, %H:%M}"


def _span(start: datetime, end: datetime) -> str:
    return f"{local(start):%a %d %b, %H:%M}-{local(end):%H:%M} IST"


def _student(session: Session, identity: GroundedIdentity) -> Optional[Student]:
    if identity.role != UserRole.STUDENT or not identity.student_code:
        return None
    return session.execute(select(Student).where(Student.student_code == identity.student_code)).scalars().first()


def _course_label(code: str, title: str) -> str:
    return f"{title} ({code})"


# --- Policy evidence used by deterministic rules ------------------------------------------------------------------


def _to_evidence(chunk: KnowledgeChunk) -> Evidence:
    return Evidence(evidence_id=f"ev-{chunk.source_id}-{chunk.section[:20]}", document_id=chunk.document_id,
                    title=chunk.title, source=chunk.source_id, snippet=chunk.text, section=chunk.section,
                    relevance_score=chunk.score)


def attendance_threshold(lookup: KnowledgeLookup) -> Tuple[Optional[Decimal], List[KnowledgeChunk]]:
    """The required attendance percentage, extracted deterministically from the retrieved attendance policy."""
    chunks = [c for c in lookup("minimum attendance requirement percentage", ["attendance_policy"], 3)
              if c.section.lower().startswith("minimum attendance requirement")]
    threshold = extract_attendance_threshold([_to_evidence(c) for c in chunks])
    if threshold.status != ThresholdExtractionStatus.OK or threshold.required_percentage is None:
        return None, []
    return threshold.required_percentage, chunks[:1]


# --- Student ---------------------------------------------------------------------------------------------------


def student_timetable(session: Session, identity: GroundedIdentity, now: datetime, question: str) -> FactResult:
    student = _student(session, identity)
    if student is None:
        return FactResult()
    assignments = student_assignments(session, student)
    today = local(now).date()
    days = range(7) if re.search(r"\b(week|weekly)\b", question.lower()) else range(1)
    lines: List[str] = []
    result = FactResult()
    next_class = None
    for offset in days:
        day = today + timedelta(days=offset)
        classes = [c for c in planned_classes(session, assignments, day) if c.status != AttendanceSessionStatus.CANCELLED.value]
        label = "Today" if offset == 0 else f"{day:%A}"
        entries = [f"{local(c.start):%H:%M}-{local(c.end):%H:%M} {c.assignment.course.title} ({c.assignment.course.code}), "
                   f"{c.room}{' (extra class)' if c.is_extra else ''}" for c in classes]
        result.facts.append((f"{label} {day:%a %d %b} classes", entries or "none"))
        lines.append(f"{label} ({day:%a %d %b}):" + ("".join(f"\n- {e}" for e in entries) if entries else " no classes"))
        if offset == 0:
            next_class = next((c for c in classes if c.start > now), None)
    if next_class is None:
        for offset in range(1, 8):
            classes = planned_classes(session, assignments, today + timedelta(days=offset))
            if classes:
                next_class = classes[0]
                break
    if next_class is not None:
        nxt = f"{next_class.assignment.course.title} ({next_class.assignment.course.code}) at {_when(next_class.start)}, {next_class.room}"
        result.facts.append(("Next class", nxt))
        lines.append(f"Next class: {nxt}.")
    result.draft = "Your timetable:\n" + "\n".join(lines)
    result.found = bool(assignments)
    return result


def student_attendance(session: Session, identity: GroundedIdentity, lookup: KnowledgeLookup) -> FactResult:
    student = _student(session, identity)
    if student is None:
        return FactResult()
    snapshots = academic_service.get_attendance(session, student.student_code)
    if not snapshots:
        return FactResult()
    required, sources = attendance_threshold(lookup)
    result = FactResult(found=True, sources=sources)
    lines = []
    for s in snapshots:
        calc = compute_attendance(s.classes_attended, s.classes_conducted, required if required is not None else Decimal(0))
        if calc.status != AttendanceRuleStatus.OK:
            line = f"{_course_label(s.course_code, s.course_title)}: no classes conducted yet"
        else:
            line = f"{_course_label(s.course_code, s.course_title)}: {s.classes_attended}/{s.classes_conducted} = {calc.current_percentage}%"
            if required is not None and not calc.eligible_now:
                line += f" (below {required}%; attend the next {calc.classes_needed_to_reach_threshold} classes to reach it)"
        result.facts.append((s.course_code, line))
        lines.append(f"- {line}")
    if required is not None:
        result.facts.append(("Required attendance (attendance policy)", f"{required}%"))
    header = "Your attendance by course" + (f" (required: {required}% per course)" if required is not None else "")
    result.draft = header + ":\n" + "\n".join(lines)
    return result


def _exam_title(exam: Exam) -> str:
    return exam.title or f"{exam.course.title} {exam.exam_type}"


def student_exams(session: Session, identity: GroundedIdentity, now: datetime) -> FactResult:
    student = _student(session, identity)
    if student is None:
        return FactResult()
    upcoming = sorted((e for e in exam_rows_for_student(session, student.student_code) if e.scheduled_end > now),
                      key=lambda e: e.scheduled_start)
    if not upcoming:
        return FactResult(found=False)
    first = upcoming[0]
    result = FactResult(found=True)
    nxt = f"{_exam_title(first)} - {_course_label(first.course.code, first.course.title)}, {_span(first.scheduled_start, first.scheduled_end)}, {first.location}"
    result.facts.append(("Next exam", nxt))
    others = [f"{_exam_title(e)} ({e.course.code}), {_span(e.scheduled_start, e.scheduled_end)}, {e.location}"
              for e in upcoming[1:MAX_LIST]]
    if others:
        result.facts.append(("Later exams", others))
    result.must_mention = [_exam_title(first)]
    result.draft = f"Your next exam is {nxt}." + ("\nAlso upcoming:" + "".join(f"\n- {o}" for o in others) if others else "")
    return result


def student_assignments_status(session: Session, identity: GroundedIdentity, now: datetime) -> FactResult:
    student = _student(session, identity)
    if student is None:
        return FactResult()
    actor = SimpleNamespace(role=UserRole.STUDENT, student_code=student.student_code)
    rows = assignment_service.my_assignments(session, actor)
    if not rows:
        return FactResult()
    pending = [r for r in rows if r.submission_status is None and r.status == AssignmentStatus.PUBLISHED]
    done = [r for r in rows if r.submission_status is not None]
    missed = [r for r in rows if r.submission_status is None and r.status == AssignmentStatus.CLOSED]
    result = FactResult(found=True)
    pending_lines = [f"{r.title} ({r.course_code}), due {_when(r.deadline_at)} IST"
                     + (" - deadline passed" if r.deadline_at < now else "") for r in pending]
    result.facts.append(("Pending assignments", pending_lines or "none"))
    result.facts.append(("Submitted assignments", [f"{r.title} ({r.course_code}): {r.submission_status.value}"
                                                   for r in done[:MAX_LIST]] or "none"))
    if missed:
        result.facts.append(("Closed without a submission", [f"{r.title} ({r.course_code})" for r in missed]))
    result.must_mention = [r.title for r in pending]
    text = (f"You have {len(pending)} pending assignment{'s' if len(pending) != 1 else ''}:"
            + "".join(f"\n- {line}" for line in pending_lines)) if pending else "You have no pending assignments."
    text += f"\nSubmitted: {len(done)}" + (f" ({', '.join(r.title for r in done[:3])})" if done else "") + "."
    if missed:
        text += f"\nClosed without your submission: {', '.join(r.title for r in missed)}."
    result.draft = text
    return result


_COURSE_ALIASES = {"os": "operating systems", "dbms": "database management systems", "cn": "computer networks",
                   "networks": "computer networks", "se": "software engineering", "ds": "data structures"}


def resolve_course(session: Session, student: Student, question: str) -> Tuple[Optional[Course], List[Course]]:
    """The enrolled course the question names (code, title or common abbreviation), plus all enrolled courses."""
    courses = list(session.execute(select(Course).join(Enrollment, Enrollment.course_id == Course.id)
                                   .where(Enrollment.student_id == student.id).order_by(Course.code)).scalars())
    text = question.lower()
    words = set(re.findall(r"[a-z0-9]+", text))
    for course in courses:
        if course.code.lower() in words or course.title.lower() in text:
            return course, courses
    for alias, title in _COURSE_ALIASES.items():
        if alias in words:
            match = next((c for c in courses if c.title.lower() == title), None)
            if match is not None:
                return match, courses
    return None, courses


def student_exam_eligibility(session: Session, identity: GroundedIdentity, now: datetime, question: str,
                             lookup: KnowledgeLookup) -> FactResult:
    student = _student(session, identity)
    if student is None:
        return FactResult()
    course, courses = resolve_course(session, student, question)
    if course is None:
        names = ", ".join(_course_label(c.code, c.title) for c in courses)
        return FactResult(found=bool(courses), facts=[("Enrolled courses", names)],
                          draft=f"Which course do you mean? You are enrolled in: {names}.")
    record = session.execute(select(AttendanceRecord).join(Enrollment, Enrollment.id == AttendanceRecord.enrollment_id)
                             .where(Enrollment.student_id == student.id, Enrollment.course_id == course.id)).scalar_one_or_none()
    required, sources = attendance_threshold(lookup)
    if record is None or required is None:
        return FactResult()
    calc = compute_attendance(record.classes_attended, record.classes_conducted, required)
    verdict = compute_exam_eligibility(calc)
    exam_rules = [c for c in lookup("eligibility end-semester examination attendance fee dues", ["exam_regulations"], 2)
                  if "eligib" in c.section.lower()]
    result = FactResult(found=True, sources=sources + exam_rules[:1])
    label = _course_label(course.code, course.title)
    result.facts += [
        ("Course", label),
        ("Attendance", f"{record.classes_attended}/{record.classes_conducted} = {calc.current_percentage}%"),
        ("Required attendance (attendance policy)", f"{required}%"),
        ("Attendance-based exam eligibility (rule)", verdict.status.value),
    ]
    upcoming = sorted((e for e in exam_rows_for_student(session, student.student_code)
                       if e.course_id == course.id and e.scheduled_end > now), key=lambda e: e.scheduled_start)
    if upcoming:
        result.facts.append(("Next exam in this course", f"{_exam_title(upcoming[0])}, {_span(upcoming[0].scheduled_start, upcoming[0].scheduled_end)}"))
    if verdict.status == ExamEligibilityStatus.ELIGIBLE:
        text = (f"Yes - on attendance you are eligible for the {label} end-semester exam: "
                f"{record.classes_attended}/{record.classes_conducted} classes = {calc.current_percentage}%, "
                f"at or above the required {required}%.")
    elif verdict.status == ExamEligibilityStatus.NOT_ELIGIBLE:
        needed = calc.classes_needed_to_reach_threshold
        result.facts.append(("Classes needed to reach the requirement", needed))
        text = (f"Not yet - on attendance you are not currently eligible for the {label} end-semester exam: "
                f"{record.classes_attended}/{record.classes_conducted} classes = {calc.current_percentage}%, "
                f"below the required {required}%."
                + (f" Attending the next {needed} classes would bring you to {required}%." if calc.threshold_reachable
                   else " The requirement can no longer be reached this semester."))
    else:
        text = f"Your attendance eligibility for {label} could not be determined from the records."
    if upcoming:
        text += f" Next exam in this course: {_exam_title(upcoming[0])}, {_span(upcoming[0].scheduled_start, upcoming[0].scheduled_end)}."
    if exam_rules:
        text += " The examination regulations also require no pending fee dues; fee status is not checked here."
    result.draft = text
    return result


def student_placements(session: Session, identity: GroundedIdentity, now: datetime) -> FactResult:
    student = _student(session, identity)
    profile = career_service.get_career_profile(session, student.student_code) if student else None
    if profile is None:
        return FactResult()
    applications = {a.opportunity_title: a.status for a in career_service.get_applications(session, student.student_code)}
    checks = [compute_eligibility(profile, o, now=now, already_applied=o.title in applications,
                                  application_status=applications.get(o.title))
              for o in career_service.get_open_opportunities(session)]
    eligible = sorted((c for c in checks if c.status.value == "eligible"), key=lambda c: c.opportunity.deadline)
    result = FactResult(found=bool(checks) or bool(applications))
    lines = [f"{c.opportunity.title} at {c.opportunity.company} ({c.opportunity.opportunity_type}), min CGPA "
             f"{c.opportunity.minimum_cgpa}, apply by {local(c.opportunity.deadline):%d %b}"
             + (f" - already applied ({c.application_status})" if c.already_applied else "") for c in eligible[:MAX_LIST]]
    result.facts += [("Your profile", f"{profile.department_code}, year {profile.year}, CGPA {profile.cgpa}"),
                     ("Open opportunities you are eligible for", lines or "none"),
                     ("Your applications", [f"{t}: {s}" for t, s in applications.items()] or "none")]
    result.must_mention = [c.opportunity.title for c in eligible[:3]]
    result.draft = (f"Open opportunities you are eligible for ({len(eligible)}):" + "".join(f"\n- {line}" for line in lines)
                    if lines else "There are no open opportunities you are currently eligible for.")
    if applications:
        result.draft += "\nYour applications: " + "; ".join(f"{t} - {s.replace('_', ' ')}" for t, s in applications.items()) + "."
    return result


def student_events(session: Session, identity: GroundedIdentity, now: datetime) -> FactResult:
    student = _student(session, identity)
    events = [e for e in events_service.get_upcoming_events(session, now) if e.status in ("open", "scheduled")]
    if not events:
        return FactResult()
    result = FactResult(found=True)
    lines = []
    for event in sorted(events, key=lambda e: e.start_at)[:MAX_LIST]:
        status = (events_service.get_student_registration_status(session, student.student_code, event.event_id)
                  if student else None)
        lines.append(f"{event.title} ({event.category}), {_span(event.start_at, event.end_at)}, {event.location}"
                     + (f" - you are registered ({status})" if status else ""))
    result.facts.append(("Upcoming campus events", lines))
    result.must_mention = [e.title for e in sorted(events, key=lambda e: e.start_at)[:2]]
    result.draft = "Upcoming campus events:" + "".join(f"\n- {line}" for line in lines)
    return result


def student_cases(session: Session, identity: GroundedIdentity) -> FactResult:
    student = _student(session, identity)
    cases = services_service.get_student_case_summaries(session, student.student_code) if student else []
    if not cases:
        return FactResult()
    lines = [f"{c.case_code} ({c.category}, {c.priority} priority): {c.status} - {c.description[:80]}" for c in cases[:MAX_LIST]]
    return FactResult(found=True, facts=[("Your campus service cases", lines)], must_mention=[c.case_code for c in cases[:MAX_LIST]],
                      draft="Your campus service cases:" + "".join(f"\n- {line}" for line in lines))


# --- Faculty / HOD (their own classes only) -------------------------------------------------------------------------


def _taught(session: Session, identity: GroundedIdentity) -> List[TeachingAssignment]:
    if identity.role not in (UserRole.FACULTY, UserRole.HOD) or identity.faculty_profile_id is None:
        return []
    return list(session.execute(select(TeachingAssignment).where(
        TeachingAssignment.faculty_id == identity.faculty_profile_id).order_by(TeachingAssignment.id)).scalars())


def _student_name(student: Student) -> str:
    return student.user.full_name if student.user is not None else student.student_code


def faculty_latest_assignment(session: Session, identity: GroundedIdentity, question: str) -> FactResult:
    taught = _taught(session, identity)
    if not taught:
        return FactResult()
    query = (select(Assignment).where(
        or_(Assignment.teaching_assignment_id.in_([t.id for t in taught]),
            Assignment.created_by_faculty_id == identity.faculty_profile_id),
        Assignment.status.in_([AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED]))
        .order_by(Assignment.published_at.desc(), Assignment.id.desc()))
    candidates = list(session.execute(query).scalars())
    codes = {c.lower() for c in re.findall(r"\b[a-z]{2}\d{3}\b", question.lower())}
    if codes:
        candidates = [a for a in candidates if session.get(Course, a.course_id).code.lower() in codes]
    if not candidates:
        return FactResult()
    assignment = candidates[0]
    course = session.get(Course, assignment.course_id)
    progress = assignment_service.compute_progress(session, assignment)
    pending = [session.get(Student, sid) for sid in progress.pending_ids]
    names = [f"{_student_name(s)} ({s.student_code})" for s in pending]
    result = FactResult(found=True, must_mention=[_student_name(s) for s in pending])
    result.facts += [
        ("Assignment", f"{assignment.title} - {_course_label(course.code, course.title)}, {assignment.status.value}, "
                       f"due {_when(assignment.deadline_at)} IST"),
        ("Submitted", f"{progress.submitted_count} of {progress.target_count} (late: {progress.late_count})"),
        ("Not submitted", names or "none"),
    ]
    head = (f"Latest assignment: {assignment.title} ({course.code}), due {_when(assignment.deadline_at)} IST. "
            f"{progress.submitted_count} of {progress.target_count} students have submitted"
            + (f", {progress.late_count} late" if progress.late_count else "") + ".")
    result.draft = head + (f"\nNot yet submitted ({len(names)}):" + "".join(f"\n- {n}" for n in names)
                           if names else "\nEvery targeted student has submitted.")
    return result


def faculty_attendance_summary(session: Session, identity: GroundedIdentity, lookup: KnowledgeLookup) -> FactResult:
    taught = _taught(session, identity)
    if not taught:
        return FactResult()
    required, sources = attendance_threshold(lookup)
    result = FactResult(found=True, sources=sources)
    lines = []
    for teaching in taught:
        sessions = list(session.execute(select(AttendanceSession.id).where(
            AttendanceSession.teaching_assignment_id == teaching.id,
            AttendanceSession.status == AttendanceSessionStatus.CLOSED)).scalars())
        marks = dict(session.execute(select(SessionAttendanceMark.status, func.count()).where(
            SessionAttendanceMark.session_id.in_(sessions or [-1])).group_by(SessionAttendanceMark.status)).all())
        total = sum(marks.values())
        attended = marks.get(AttendanceMarkStatus.PRESENT, 0) + marks.get(AttendanceMarkStatus.LATE, 0)
        rate = compute_attendance(attended, total, Decimal(0)).current_percentage if total else None
        below: List[str] = []
        if required is not None:
            for student in roster(session, teaching):
                record = session.execute(select(AttendanceRecord).join(Enrollment, Enrollment.id == AttendanceRecord.enrollment_id)
                                         .where(Enrollment.student_id == student.id,
                                                Enrollment.course_id == teaching.course_id)).scalar_one_or_none()
                if record is None:
                    continue
                calc = compute_attendance(record.classes_attended, record.classes_conducted, required)
                if calc.status == AttendanceRuleStatus.OK and not calc.eligible_now:
                    below.append(f"{_student_name(student)} {calc.current_percentage}%")
        line = (f"{_course_label(teaching.course.code, teaching.course.title)} section {teaching.section}: "
                f"{len(sessions)} sessions held recently, attendance {rate}% in those sessions" if total else
                f"{_course_label(teaching.course.code, teaching.course.title)} section {teaching.section}: no sessions recorded yet")
        if below:
            line += f"; below {required}% for the semester: {', '.join(below)}"
        result.facts.append((teaching.course.code, line))
        lines.append(f"- {line}")
    result.draft = "Attendance in your classes:\n" + "\n".join(lines)
    return result


def faculty_exams(session: Session, identity: GroundedIdentity, now: datetime) -> FactResult:
    taught = _taught(session, identity)
    if not taught:
        return FactResult()
    exams = list(session.execute(select(Exam).where(or_(
        Exam.teaching_assignment_id.in_([t.id for t in taught]),
        (Exam.teaching_assignment_id.is_(None)) & (Exam.course_id.in_([t.course_id for t in taught]))))
        .order_by(Exam.scheduled_start)).scalars())
    if not exams:
        return FactResult()
    upcoming = [e for e in exams if e.scheduled_end > now and (e.status is None or e.status.value != "cancelled")]
    held = [e for e in exams if e.scheduled_end <= now]
    result = FactResult(found=True, must_mention=[_exam_title(e) for e in upcoming[:3]])
    up_lines = []
    for exam in upcoming[:MAX_LIST]:
        targets = session.execute(select(func.count(ExamTarget.id)).where(ExamTarget.exam_id == exam.id)).scalar_one()
        up_lines.append(f"{_exam_title(exam)} ({exam.course.code}), {_span(exam.scheduled_start, exam.scheduled_end)}, "
                        f"{exam.location}" + (f", {targets} students" if targets else ""))
    held_lines = []
    for exam in held[-3:]:
        counts = dict(session.execute(select(ExamAttendance.status, func.count()).where(
            ExamAttendance.exam_id == exam.id).group_by(ExamAttendance.status)).all())
        held_lines.append(f"{_exam_title(exam)} ({exam.course.code}), {local(exam.scheduled_start):%d %b}: "
                          f"{counts.get(ExamAttendanceStatus.PRESENT, 0)} present, {counts.get(ExamAttendanceStatus.ABSENT, 0)} absent")
    result.facts += [("Upcoming exams in your classes", up_lines or "none"), ("Recently held", held_lines or "none")]
    result.draft = ("Upcoming exams in your classes:" + "".join(f"\n- {line}" for line in up_lines) if up_lines
                    else "No upcoming exams in your classes.")
    if held_lines:
        result.draft += "\nRecently held:" + "".join(f"\n- {line}" for line in held_lines)
    return result


def faculty_timetable(session: Session, identity: GroundedIdentity, now: datetime) -> FactResult:
    taught = _taught(session, identity)
    if not taught:
        return FactResult()
    classes = [c for c in planned_classes(session, taught, local(now).date())
               if c.status != AttendanceSessionStatus.CANCELLED.value]
    lines = [f"{local(c.start):%H:%M}-{local(c.end):%H:%M} {c.assignment.course.title} ({c.assignment.course.code}) "
             f"section {c.assignment.section}, {c.room}: {c.status}{' (extra class)' if c.is_extra else ''}" for c in classes]
    return FactResult(found=True, facts=[(f"Your classes today ({local(now):%a %d %b})", lines or "none")],
                      draft=(f"Your classes today ({local(now):%a %d %b}):" + "".join(f"\n- {line}" for line in lines))
                      if lines else f"You have no classes scheduled today ({local(now):%A}).")


# --- Institutional knowledge -------------------------------------------------------------------------------------


def knowledge_answer(chunks: List[KnowledgeChunk]) -> FactResult:
    if not chunks:
        return FactResult()
    shown = chunks[:2]  # the answer quotes these, so only these are cited
    parts = [f"{c.title} - {c.section}: {c.text}" for c in shown]
    return FactResult(found=True, sources=shown, draft="\n\n".join(parts))
