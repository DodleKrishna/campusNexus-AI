"""Idempotent seed data for CampusNexus AI (fictional campus, fictional students).

Running this script multiple times must not duplicate rows: every entity is
inserted via ``get_or_create``, keyed on its natural/unique fields, so a
second run is a no-op against already-seeded data.

Datetime strategy: the campus operates on India Standard Time (UTC+5:30) for
wall-clock scheduling. Seed data authors instants in IST; the ORM's
``UTCDateTime`` column type normalizes everything to UTC for storage (see
app/db/base.py), so every value read back out is timezone-aware UTC.

Usage:
    python scripts/seed_data.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (  # noqa: F401  (import registers all mapped classes)
    AttendanceRecord,
    Application,
    ApplicationStatus,
    CalendarEvent,
    CampusCase,
    CaseAssignment,
    CasePriority,
    CaseSLA,
    CaseStatus,
    Club,
    Company,
    Course,
    Department,
    Enrollment,
    Event,
    EventRegistration,
    EventStatus,
    Exam,
    FacultyProfile,
    Notification,
    NotificationStatus,
    Opportunity,
    OpportunityDepartment,
    OpportunityEligibleYear,
    OpportunitySkill,
    OpportunityStatus,
    OpportunityType,
    RegistrationStatus,
    Skill,
    Student,
    StudentSkill,
    TeachingAssignment,
    TimetableSlot,
    User,
)
from app.auth.accounts import DEV_ACCOUNTS, resolve_seed_password, seed_dev_accounts
from app.db.tenancy import assign_unowned_rows, ensure_default_organization
from app.services.agent_deployments import ensure_default_deployments
from app.db.session import create_db_engine, create_session_factory, init_db
from app.schemas.enums import UserRole

IST = timezone(timedelta(hours=5, minutes=30))
SEED_NOW = datetime.now(IST)
ACADEMIC_YEAR = "2025-2026"

DEMO_STUDENT_CODE = "STU-DEMO-001"


# ---------------------------------------------------------------------------
# Generic idempotent get-or-create
# ---------------------------------------------------------------------------


def get_or_create(session: Session, model: type, defaults: Optional[dict] = None, **lookup: object):
    instance = session.execute(select(model).filter_by(**lookup)).scalar_one_or_none()
    if instance is not None:
        return instance, False
    params = dict(lookup)
    params.update(defaults or {})
    instance = model(**params)
    session.add(instance)
    session.flush()
    return instance, True


def next_weekday_on_or_after(base: date, weekday: int) -> date:
    """The next date >= base whose weekday matches (0=Monday..6=Sunday)."""
    delta = (weekday - base.weekday()) % 7
    return base + timedelta(days=delta)


def ist(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# ---------------------------------------------------------------------------
# 1. Departments
# ---------------------------------------------------------------------------

DEPARTMENTS = [
    ("CSE", "Computer Science & Engineering"),
    ("ECE", "Electronics & Communication Engineering"),
    ("MECH", "Mechanical Engineering"),
    ("CIVIL", "Civil Engineering"),
]


def seed_departments(session: Session) -> dict[str, Department]:
    result = {}
    for code, name in DEPARTMENTS:
        dept, _ = get_or_create(session, Department, code=code, defaults={"name": name})
        result[code] = dept
    return result


# ---------------------------------------------------------------------------
# 2. Users / Students
# ---------------------------------------------------------------------------

# (student_code, full_name, dept_code, year, semester, cgpa, interests, career_goal)
STUDENT_SEED: list[tuple[str, str, str, int, int, float, str, str]] = [
    (
        DEMO_STUDENT_CODE,
        "Aditi Rao",
        "CSE",
        3,
        5,
        7.8,
        "artificial intelligence, web development, robotics",
        "Software Engineer (AI/ML)",
    ),
    ("STU2023002", "Rohan Mehta", "CSE", 3, 5, 8.9, "competitive programming, backend systems", "Backend Engineer"),
    ("STU2023003", "Sanya Kapoor", "CSE", 3, 5, 6.2, "UI/UX, mobile apps", "Product Designer"),
    ("STU2023004", "Vikram Nair", "CSE", 2, 3, 7.1, "cybersecurity", "Security Analyst"),
    ("STU2023005", "Priya Sharma", "CSE", 4, 7, 8.3, "machine learning, data science", "ML Engineer"),
    ("STU2023006", "Arjun Desai", "CSE", 1, 1, 7.5, "web development", "Full-Stack Developer"),
    ("STU2023007", "Meera Pillai", "CSE", 3, 5, 9.1, "cloud computing, devops", "Cloud Engineer"),
    ("STU2023008", "Karan Bhatt", "CSE", 2, 3, 6.8, "game development", "Game Developer"),
    ("STU2023009", "Divya Menon", "ECE", 3, 5, 7.6, "embedded systems, IoT", "Embedded Systems Engineer"),
    ("STU2023010", "Aman Gupta", "ECE", 3, 5, 8.0, "VLSI design", "Chip Design Engineer"),
    ("STU2023011", "Neha Joshi", "ECE", 2, 3, 6.9, "signal processing", "DSP Engineer"),
    ("STU2023012", "Rahul Verma", "ECE", 4, 7, 7.4, "telecommunications", "Network Engineer"),
    ("STU2023013", "Ishita Reddy", "ECE", 1, 1, 8.2, "robotics", "Robotics Engineer"),
    ("STU2023014", "Siddharth Rao", "MECH", 3, 5, 7.0, "automotive design", "Design Engineer"),
    ("STU2023015", "Ananya Iyer", "MECH", 2, 3, 6.5, "thermodynamics, energy systems", "Energy Analyst"),
    ("STU2023016", "Kabir Malhotra", "MECH", 3, 5, 8.6, "robotics, manufacturing", "Manufacturing Engineer"),
    ("STU2023017", "Tanvi Kulkarni", "MECH", 1, 1, 7.2, "CAD design", "Design Engineer"),
    ("STU2023018", "Yash Chatterjee", "CIVIL", 3, 5, 7.3, "structural engineering", "Structural Engineer"),
    ("STU2023019", "Riya Sen", "CIVIL", 2, 3, 6.7, "urban planning", "Urban Planner"),
    ("STU2023020", "Devansh Bose", "CIVIL", 3, 5, 8.1, "construction management", "Site Engineer"),
]


def seed_students(session: Session, departments: dict[str, Department]) -> dict[str, Student]:
    result: dict[str, Student] = {}
    for student_code, full_name, dept_code, year, semester, cgpa, interests, career_goal in STUDENT_SEED:
        email = f"{student_code.lower()}@meridian.edu"
        user, _ = get_or_create(
            session,
            User,
            email=email,
            defaults={"full_name": full_name, "role": UserRole.STUDENT},
        )
        student, _ = get_or_create(
            session,
            Student,
            student_code=student_code,
            defaults={
                "user_id": user.id,
                "department_id": departments[dept_code].id,
                "year": year,
                "semester": semester,
                "cgpa": cgpa,
                "interests": interests,
                "career_goal": career_goal,
            },
        )
        result[student_code] = student
    return result


# ---------------------------------------------------------------------------
# 3. Courses
# ---------------------------------------------------------------------------

# (code, title, dept_code, credits, semester, instructor)
COURSE_SEED: list[tuple[str, str, str, int, int, str]] = [
    ("CS201", "Data Structures", "CSE", 4, 3, "Dr. Kavita Iyer"),
    ("CS301", "Operating Systems", "CSE", 4, 5, "Dr. Manoj Pillai"),
    ("CS302", "Database Management Systems", "CSE", 4, 5, "Dr. Sunita Rao"),
    ("CS303", "Computer Networks", "CSE", 3, 5, "Dr. Ashok Verma"),
    ("CS304", "Software Engineering", "CSE", 3, 5, "Dr. Leela Nair"),
    ("CS401", "Machine Learning", "CSE", 3, 7, "Dr. Nikhil Bhatt"),
    ("EC201", "Digital Electronics", "ECE", 4, 3, "Dr. Ramesh Kumar"),
    ("EC301", "Signals and Systems", "ECE", 4, 5, "Dr. Anjali Menon"),
    ("EC302", "Microprocessors", "ECE", 3, 5, "Dr. Suresh Pillai"),
    ("ME201", "Thermodynamics", "MECH", 4, 3, "Dr. Ganesh Iyer"),
    ("ME301", "Fluid Mechanics", "MECH", 4, 5, "Dr. Vinod Rao"),
    ("CE201", "Surveying", "CIVIL", 3, 3, "Dr. Meenakshi Nair"),
    ("CE301", "Structural Analysis", "CIVIL", 4, 5, "Dr. Prakash Reddy"),
]


def seed_courses(session: Session, departments: dict[str, Department]) -> dict[str, Course]:
    result: dict[str, Course] = {}
    for code, title, dept_code, credits, semester, instructor in COURSE_SEED:
        course, _ = get_or_create(
            session,
            Course,
            code=code,
            defaults={
                "title": title,
                "department_id": departments[dept_code].id,
                "credits": credits,
                "semester": semester,
                "instructor": instructor,
            },
        )
        result[code] = course
    return result


# ---------------------------------------------------------------------------
# 4. Enrollments, attendance, timetable, exams
# ---------------------------------------------------------------------------

# Department -> courses a student in that department currently takes,
# keyed by the student's current semester bucket (3, 5, or 7).
DEPT_SEMESTER_COURSES: dict[str, dict[int, list[str]]] = {
    "CSE": {1: [], 3: ["CS201"], 5: ["CS301", "CS302", "CS303", "CS304"], 7: ["CS401"]},
    "ECE": {1: [], 3: ["EC201"], 5: ["EC301", "EC302"], 7: []},
    "MECH": {1: [], 3: ["ME201"], 5: ["ME301"]},
    "CIVIL": {1: [], 3: ["CE201"], 5: ["CE301"]},
}

# Demo student's OS course must land at approximately 68% attendance from raw
# counts (34/50 = 68.0%), never a stored percentage.
ATTENDANCE_PLAN: dict[str, tuple[int, int]] = {
    "CS301": (34, 50),  # ~68% -- attendance shortage scenario
    "CS302": (47, 50),
    "CS303": (44, 50),
    "CS304": (40, 50),
}
DEFAULT_ATTENDANCE = (42, 48)

# (course_code, weekday[0=Mon], start, end, location)
TIMETABLE_SEED: list[tuple[str, int, time, time, str]] = [
    ("CS201", 1, time(9, 0), time(10, 0), "Block A - Room 101"),
    ("CS301", 0, time(10, 0), time(11, 0), "Block A - Room 204"),
    ("CS302", 1, time(10, 0), time(11, 0), "Block A - Room 205"),
    ("CS303", 2, time(10, 0), time(11, 0), "Block A - Room 206"),
    ("CS304", 3, time(10, 0), time(11, 0), "Block A - Room 207"),
    ("CS401", 4, time(9, 0), time(10, 30), "Block A - Room 301"),
    ("EC201", 1, time(11, 0), time(12, 0), "Block B - Room 101"),
    ("EC301", 0, time(9, 0), time(10, 0), "Block B - Room 201"),
    ("EC302", 2, time(9, 0), time(10, 0), "Block B - Room 202"),
    ("ME201", 1, time(9, 0), time(10, 0), "Block C - Room 101"),
    ("ME301", 3, time(9, 0), time(10, 0), "Block C - Room 201"),
    ("CE201", 1, time(13, 0), time(14, 0), "Block D - Room 101"),
    ("CE301", 4, time(13, 0), time(14, 0), "Block D - Room 201"),
]

# (course_code, exam_type, weekday_offset_days_from_seed_now, start_time, end_time, location)
EXAM_SEED: list[tuple[str, str, int, time, time, str]] = [
    ("CS201", "midterm", 20, time(10, 0), time(12, 0), "Exam Hall 1"),
    ("CS301", "midterm", 22, time(10, 0), time(12, 0), "Exam Hall 1"),
    ("CS302", "midterm", 24, time(14, 0), time(16, 0), "Exam Hall 2"),
    ("CS303", "midterm", 26, time(10, 0), time(12, 0), "Exam Hall 1"),
    ("CS304", "midterm", 28, time(10, 0), time(12, 0), "Exam Hall 2"),
    ("EC201", "midterm", 21, time(10, 0), time(12, 0), "Exam Hall 3"),
    ("EC301", "midterm", 23, time(10, 0), time(12, 0), "Exam Hall 3"),
    ("ME201", "midterm", 25, time(10, 0), time(12, 0), "Exam Hall 4"),
    ("CE201", "midterm", 27, time(10, 0), time(12, 0), "Exam Hall 4"),
]


def seed_enrollments_and_academics(
    session: Session, students: dict[str, Student], courses: dict[str, Course]
) -> None:
    for student_code, _, dept_code, year, semester, *_ in STUDENT_SEED:
        student = students[student_code]
        course_codes = DEPT_SEMESTER_COURSES.get(dept_code, {}).get(semester, [])
        for course_code in course_codes:
            course = courses[course_code]
            enrollment, _ = get_or_create(
                session,
                Enrollment,
                student_id=student.id,
                course_id=course.id,
                academic_year=ACADEMIC_YEAR,
                defaults={"semester": semester},
            )
            attended, conducted = (
                ATTENDANCE_PLAN.get(course_code, DEFAULT_ATTENDANCE)
                if student_code == DEMO_STUDENT_CODE
                else DEFAULT_ATTENDANCE
            )
            get_or_create(
                session,
                AttendanceRecord,
                enrollment_id=enrollment.id,
                defaults={"classes_attended": attended, "classes_conducted": conducted},
            )

    for course_code, weekday, start, end, location in TIMETABLE_SEED:
        course = courses[course_code]
        get_or_create(
            session,
            TimetableSlot,
            course_id=course.id,
            weekday=weekday,
            start_time=start,
            defaults={"end_time": end, "location": location, "semester": course.semester},
        )

    base_date = SEED_NOW.date()
    for course_code, exam_type, day_offset, start, end, location in EXAM_SEED:
        course = courses[course_code]
        exam_date = base_date + timedelta(days=day_offset)
        scheduled_start = datetime.combine(exam_date, start, tzinfo=IST)
        scheduled_end = datetime.combine(exam_date, end, tzinfo=IST)
        get_or_create(
            session,
            Exam,
            course_id=course.id,
            exam_type=exam_type,
            scheduled_start=scheduled_start,
            defaults={"scheduled_end": scheduled_end, "location": location},
        )


# ---------------------------------------------------------------------------
# 5. Career domain
# ---------------------------------------------------------------------------

COMPANY_SEED = [
    ("NimbusCloud Technologies", "Software / Cloud"),
    ("Vertex Robotics", "Robotics"),
    ("BluePeak Analytics", "Data & Analytics"),
    ("Solaris Software Labs", "Software"),
    ("GreenGrid Energy Systems", "Energy"),
    ("Quantus Financial Technologies", "FinTech"),
    ("Skyline Aerospace", "Aerospace"),
    ("Civicore Infrastructure", "Civil / Construction"),
]

SKILL_SEED = [
    "Python", "Java", "C++", "Machine Learning", "Data Structures", "SQL",
    "React", "Node.js", "Cloud Computing (AWS)", "Docker", "Kubernetes",
    "Embedded Systems", "AutoCAD", "Structural Design", "Circuit Design",
    "Communication Skills", "Project Management",
]

# Demo student's skills: (skill name, proficiency 1-5)
DEMO_SKILLS = [
    ("Python", 4),
    ("Machine Learning", 3),
    ("SQL", 3),
    ("Data Structures", 4),
    ("Communication Skills", 3),
]

# A handful of skills for a few other students, for realism.
OTHER_STUDENT_SKILLS: dict[str, list[tuple[str, int]]] = {
    "STU2023002": [("Python", 5), ("Java", 4), ("Data Structures", 5)],
    "STU2023007": [("Docker", 4), ("Kubernetes", 4), ("Cloud Computing (AWS)", 5), ("Python", 3)],
    "STU2023009": [("Embedded Systems", 4), ("Circuit Design", 3)],
    "STU2023016": [("Project Management", 3)],
}


@dataclass
class OpportunitySeed:
    title: str
    company: str
    opportunity_type: OpportunityType
    minimum_cgpa: float
    allowed_departments: list[str]
    eligible_years: list[int]
    required_skills: list[tuple[str, Optional[int]]]
    deadline_days_from_now: int  # negative => already past
    status: OpportunityStatus


OPPORTUNITY_SEED: list[OpportunitySeed] = [
    # -- demo-student scenarios (CLAUDE.md Phase 2 spec section 13.B) --
    OpportunitySeed(
        "AI Software Engineering Intern", "NimbusCloud Technologies", OpportunityType.INTERNSHIP,
        7.0, ["CSE"], [3, 4], [("Python", 2), ("Machine Learning", 2)], 45, OpportunityStatus.OPEN,
    ),  # eligible
    OpportunitySeed(
        "Quant Research Intern", "Quantus Financial Technologies", OpportunityType.INTERNSHIP,
        8.5, ["CSE"], [3, 4], [("Python", 3), ("SQL", 3)], 40, OpportunityStatus.OPEN,
    ),  # cgpa-blocked (min 8.5 > demo's 7.8)
    OpportunitySeed(
        "Embedded Systems Intern", "Vertex Robotics", OpportunityType.INTERNSHIP,
        6.5, ["ECE", "MECH"], [3, 4], [("Embedded Systems", 2)], 35, OpportunityStatus.OPEN,
    ),  # dept-blocked (CSE not in allowed departments)
    OpportunitySeed(
        "Cloud DevOps Intern", "Solaris Software Labs", OpportunityType.INTERNSHIP,
        7.0, ["CSE"], [3, 4], [("Docker", 3), ("Kubernetes", 3), ("Cloud Computing (AWS)", 3)], 50,
        OpportunityStatus.OPEN,
    ),  # skill-gap (demo has none of these skills)
    OpportunitySeed(
        "Data Analyst Intern", "BluePeak Analytics", OpportunityType.INTERNSHIP,
        6.5, ["CSE"], [2, 3, 4], [("SQL", 2), ("Python", 2)], -15, OpportunityStatus.EXPIRED,
    ),  # expired
    # -- filler opportunities for realistic variety --
    OpportunitySeed(
        "Full-Stack Developer Intern", "Solaris Software Labs", OpportunityType.INTERNSHIP,
        6.0, ["CSE"], [2, 3, 4], [("React", 1), ("Node.js", 1)], 30, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "Robotics Software Intern", "Vertex Robotics", OpportunityType.INTERNSHIP,
        7.0, ["CSE", "ECE"], [3, 4], [("Python", 2)], 38, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "Backend Engineer", "NimbusCloud Technologies", OpportunityType.JOB,
        7.5, ["CSE"], [4], [("Python", 3), ("SQL", 2)], 60, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "Circuit Design Intern", "Vertex Robotics", OpportunityType.INTERNSHIP,
        6.5, ["ECE"], [3, 4], [("Circuit Design", 2)], 33, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "VLSI Design Trainee", "Skyline Aerospace", OpportunityType.INTERNSHIP,
        7.0, ["ECE"], [3, 4], [("Circuit Design", 2)], 42, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "Mechanical Design Intern", "GreenGrid Energy Systems", OpportunityType.INTERNSHIP,
        6.0, ["MECH"], [2, 3, 4], [("AutoCAD", 2)], 36, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "Energy Systems Analyst", "GreenGrid Energy Systems", OpportunityType.JOB,
        7.0, ["MECH", "CIVIL"], [4], [("Project Management", 1)], 55, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "Aerospace Systems Intern", "Skyline Aerospace", OpportunityType.INTERNSHIP,
        7.5, ["MECH", "ECE"], [3, 4], [("AutoCAD", 2)], 48, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "Structural Engineer Trainee", "Civicore Infrastructure", OpportunityType.INTERNSHIP,
        6.0, ["CIVIL"], [2, 3, 4], [("Structural Design", 2)], 32, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "Civil Site Engineer Trainee", "Civicore Infrastructure", OpportunityType.INTERNSHIP,
        6.0, ["CIVIL"], [3, 4], [("Project Management", 1)], 29, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "Financial Analyst Intern", "Quantus Financial Technologies", OpportunityType.INTERNSHIP,
        6.5, ["CSE", "MECH", "CIVIL"], [3, 4], [("Communication Skills", 1)], 27, OpportunityStatus.OPEN,
    ),
    OpportunitySeed(
        "ML Research Assistant", "BluePeak Analytics", OpportunityType.JOB,
        8.0, ["CSE"], [4], [("Machine Learning", 3), ("Python", 3)], 20, OpportunityStatus.CLOSED,
    ),
]

# Applications: (student_code, opportunity_title, status)
APPLICATION_SEED = [
    (DEMO_STUDENT_CODE, "AI Software Engineering Intern", ApplicationStatus.UNDER_REVIEW),
    ("STU2023002", "Backend Engineer", ApplicationStatus.SUBMITTED),
    ("STU2023007", "Full-Stack Developer Intern", ApplicationStatus.SUBMITTED),
    ("STU2023009", "Embedded Systems Intern", ApplicationStatus.ACCEPTED),
    ("STU2023016", "Mechanical Design Intern", ApplicationStatus.REJECTED),
    ("STU2023018", "Structural Engineer Trainee", ApplicationStatus.SUBMITTED),
]


def seed_career(
    session: Session, students: dict[str, Student], departments: dict[str, Department]
) -> dict[str, Opportunity]:
    companies: dict[str, Company] = {}
    for name, industry in COMPANY_SEED:
        company, _ = get_or_create(session, Company, name=name, defaults={"industry": industry})
        companies[name] = company

    skills: dict[str, Skill] = {}
    for name in SKILL_SEED:
        skill, _ = get_or_create(session, Skill, name=name)
        skills[name] = skill

    demo = students[DEMO_STUDENT_CODE]
    for skill_name, proficiency in DEMO_SKILLS:
        get_or_create(
            session, StudentSkill, student_id=demo.id, skill_id=skills[skill_name].id,
            defaults={"proficiency": proficiency},
        )
    for student_code, skill_list in OTHER_STUDENT_SKILLS.items():
        student = students[student_code]
        for skill_name, proficiency in skill_list:
            get_or_create(
                session, StudentSkill, student_id=student.id, skill_id=skills[skill_name].id,
                defaults={"proficiency": proficiency},
            )

    opportunities: dict[str, Opportunity] = {}
    for spec in OPPORTUNITY_SEED:
        company = companies[spec.company]
        opportunity, created = get_or_create(
            session,
            Opportunity,
            company_id=company.id,
            title=spec.title,
            defaults={
                "description": f"{spec.title} at {spec.company}.",
                "opportunity_type": spec.opportunity_type,
                "minimum_cgpa": spec.minimum_cgpa,
                "deadline": SEED_NOW + timedelta(days=spec.deadline_days_from_now),
                "status": spec.status,
                "posted_at": SEED_NOW - timedelta(days=5),
            },
        )
        opportunities[spec.title] = opportunity
        if created:
            for dept_code in spec.allowed_departments:
                get_or_create(
                    session, OpportunityDepartment,
                    opportunity_id=opportunity.id, department_id=departments[dept_code].id,
                )
            for year in spec.eligible_years:
                get_or_create(session, OpportunityEligibleYear, opportunity_id=opportunity.id, year=year)
            for skill_name, min_prof in spec.required_skills:
                get_or_create(
                    session, OpportunitySkill,
                    opportunity_id=opportunity.id, skill_id=skills[skill_name].id,
                    defaults={"minimum_proficiency": min_prof},
                )

    for student_code, opp_title, status in APPLICATION_SEED:
        get_or_create(
            session, Application,
            student_id=students[student_code].id, opportunity_id=opportunities[opp_title].id,
            defaults={"status": status},
        )

    return opportunities


# ---------------------------------------------------------------------------
# 6. Events domain
# ---------------------------------------------------------------------------

CLUB_SEED = [
    ("AI/ML Club", "Explores applied AI/ML through workshops and projects.", "technology"),
    ("Robotics Club", "Builds and competes with autonomous robots.", "technology"),
    ("Coding Club", "Competitive programming and hackathons.", "technology"),
    ("Cultural Committee", "Organizes cultural fests and performances.", "culture"),
    ("Entrepreneurship Cell", "Supports student startups and pitch events.", "business"),
    ("Sports Committee", "Organizes inter-department sports events.", "sports"),
]


@dataclass
class EventSeed:
    title: str
    club: Optional[str]
    category: str
    organizer: str
    location: str
    start_at: datetime
    end_at: datetime
    registration_deadline: Optional[datetime]
    capacity: Optional[int]
    status: EventStatus


def _build_event_seed() -> list[EventSeed]:
    base_date = SEED_NOW.date()

    # Scenario A: relevant AI workshop, no academic conflict (Saturday, no classes).
    saturday = next_weekday_on_or_after(base_date + timedelta(days=7), weekday=5)
    ai_workshop = EventSeed(
        "Artificial Intelligence & Deep Learning Workshop", "AI/ML Club", "workshop", "AI/ML Club",
        "Auditorium 1",
        datetime.combine(saturday, time(14, 0), tzinfo=IST),
        datetime.combine(saturday, time(17, 0), tzinfo=IST),
        datetime.combine(saturday - timedelta(days=2), time(23, 59), tzinfo=IST),
        150, EventStatus.OPEN,
    )

    # Scenario B: overlaps demo student's CS301 (Operating Systems) timetable
    # slot -- Monday 10:00-11:00.
    monday = next_weekday_on_or_after(base_date + timedelta(days=7), weekday=0)
    hackathon_kickoff = EventSeed(
        "Hackathon Kickoff Session", "Coding Club", "competition", "Coding Club",
        "Auditorium 2",
        datetime.combine(monday, time(10, 30), tzinfo=IST),
        datetime.combine(monday, time(12, 0), tzinfo=IST),
        datetime.combine(monday - timedelta(days=3), time(23, 59), tzinfo=IST),
        200, EventStatus.OPEN,
    )

    # Scenario C: overlaps demo student's CS302 (DBMS) exam window
    # (base + 24 days, 14:00-16:00 IST -- see EXAM_SEED).
    exam_date = base_date + timedelta(days=24)
    robotics_expo = EventSeed(
        "Robotics Expo", "Robotics Club", "exhibition", "Robotics Club", "Sports Ground",
        datetime.combine(exam_date, time(15, 0), tzinfo=IST),
        datetime.combine(exam_date, time(18, 0), tzinfo=IST),
        datetime.combine(exam_date - timedelta(days=5), time(23, 59), tzinfo=IST),
        300, EventStatus.OPEN,
    )

    # Scenario D: at capacity (capacity == confirmed registrations, seeded below).
    pitch_day = next_weekday_on_or_after(base_date + timedelta(days=10), weekday=3)
    pitch_night = EventSeed(
        "Startup Pitch Night", "Entrepreneurship Cell", "networking", "Entrepreneurship Cell",
        "Seminar Hall",
        datetime.combine(pitch_day, time(18, 0), tzinfo=IST),
        datetime.combine(pitch_day, time(20, 0), tzinfo=IST),
        datetime.combine(pitch_day - timedelta(days=1), time(23, 59), tzinfo=IST),
        3, EventStatus.OPEN,
    )

    # Scenario E: expired/closed (fully in the past).
    past_day = base_date - timedelta(days=30)
    orientation = EventSeed(
        "Freshers' Orientation Meet", None, "orientation", "Dean of Students Office", "Auditorium 1",
        datetime.combine(past_day, time(10, 0), tzinfo=IST),
        datetime.combine(past_day, time(13, 0), tzinfo=IST),
        datetime.combine(past_day - timedelta(days=2), time(23, 59), tzinfo=IST),
        500, EventStatus.COMPLETED,
    )

    filler_specs = [
        ("Tech Talk: Cloud Native Systems", "AI/ML Club", "seminar", "AI/ML Club", "Seminar Hall", 12, 9, 11, 90, EventStatus.OPEN),
        ("Annual Cultural Fest", "Cultural Committee", "cultural", "Cultural Committee", "Open Air Theatre", 15, 17, 22, 1000, EventStatus.SCHEDULED),
        ("Inter-Department Sports Meet", "Sports Committee", "sports", "Sports Committee", "Sports Ground", 18, 9, 17, 400, EventStatus.SCHEDULED),
        ("Campus Career Fair", None, "career", "Career Services Office", "Auditorium 1", 33, 10, 16, 600, EventStatus.SCHEDULED),
        ("Competitive Coding Contest", "Coding Club", "competition", "Coding Club", "Computer Lab 1", 9, 15, 18, 120, EventStatus.OPEN),
        ("Blood Donation Camp", None, "community", "Student Welfare Office", "Health Centre", 6, 9, 14, 200, EventStatus.OPEN),
        ("Alumni Homecoming Meet", None, "networking", "Alumni Relations Office", "Auditorium 2", 40, 17, 21, 250, EventStatus.SCHEDULED),
        ("Entrepreneurship Summit", "Entrepreneurship Cell", "networking", "Entrepreneurship Cell", "Seminar Hall", 45, 10, 17, 300, EventStatus.SCHEDULED),
        ("Photography Contest Exhibition", "Cultural Committee", "cultural", "Cultural Committee", "Art Gallery", 14, 11, 18, 80, EventStatus.OPEN),
        ("Music Night", "Cultural Committee", "cultural", "Cultural Committee", "Open Air Theatre", 16, 19, 22, 800, EventStatus.SCHEDULED),
    ]
    fillers = []
    for title, club, category, organizer, location, day_offset, start_h, end_h, capacity, status in filler_specs:
        day = base_date + timedelta(days=day_offset)
        fillers.append(
            EventSeed(
                title, club, category, organizer, location,
                datetime.combine(day, time(start_h, 0), tzinfo=IST),
                datetime.combine(day, time(end_h, 0), tzinfo=IST),
                datetime.combine(day - timedelta(days=2), time(23, 59), tzinfo=IST),
                capacity, status,
            )
        )

    return [ai_workshop, hackathon_kickoff, robotics_expo, pitch_night, orientation, *fillers]


def seed_events(session: Session, clubs: dict[str, Club], students: dict[str, Student]) -> dict[str, Event]:
    events: dict[str, Event] = {}
    for spec in _build_event_seed():
        club_id = clubs[spec.club].id if spec.club else None
        event, _ = get_or_create(
            session,
            Event,
            title=spec.title,
            defaults={
                "club_id": club_id,
                "description": f"{spec.title}, organized by {spec.organizer}.",
                "category": spec.category,
                "organizer": spec.organizer,
                "location": spec.location,
                "start_at": spec.start_at,
                "end_at": spec.end_at,
                "registration_deadline": spec.registration_deadline,
                "capacity": spec.capacity,
                "status": spec.status,
            },
        )
        events[spec.title] = event

    # Demo student registers for the no-conflict AI workshop -- this is also
    # the "existing registration" fixture for future duplicate-prevention tests.
    get_or_create(
        session, EventRegistration,
        event_id=events["Artificial Intelligence & Deep Learning Workshop"].id,
        student_id=students[DEMO_STUDENT_CODE].id,
        defaults={"status": RegistrationStatus.CONFIRMED},
    )

    # Fill "Startup Pitch Night" to exactly its capacity (3).
    for student_code in ["STU2023002", "STU2023009", "STU2023018"]:
        get_or_create(
            session, EventRegistration,
            event_id=events["Startup Pitch Night"].id, student_id=students[student_code].id,
            defaults={"status": RegistrationStatus.CONFIRMED},
        )

    # A few more registrations elsewhere, for realism.
    for student_code, title in [
        ("STU2023007", "Tech Talk: Cloud Native Systems"),
        ("STU2023016", "Competitive Coding Contest"),
        ("STU2023003", "Annual Cultural Fest"),
    ]:
        get_or_create(
            session, EventRegistration,
            event_id=events[title].id, student_id=students[student_code].id,
            defaults={"status": RegistrationStatus.CONFIRMED},
        )

    return events


def seed_clubs(session: Session) -> dict[str, Club]:
    clubs: dict[str, Club] = {}
    for name, description, category in CLUB_SEED:
        club, _ = get_or_create(session, Club, name=name, defaults={"description": description, "category": category})
        clubs[name] = club
    return clubs


# ---------------------------------------------------------------------------
# 7. Campus services (cases + SLA)
# ---------------------------------------------------------------------------


@dataclass
class CaseSeed:
    case_code: str
    student_code: str
    category: str
    description: str
    priority: CasePriority
    department: str
    status: CaseStatus
    created_days_ago: int
    response_hours: int
    resolution_hours: int
    resolved_days_ago: Optional[int]  # None => unresolved


CASE_SEED: list[CaseSeed] = [
    CaseSeed(
        "CASE-0001", DEMO_STUDENT_CODE, "hostel", "Leaking tap in room B-204 washroom.",
        CasePriority.NORMAL, "Hostel Office", CaseStatus.OPEN, 2, 24, 120, None,
    ),
    CaseSeed(
        "CASE-0002", DEMO_STUDENT_CODE, "it_helpdesk",
        "Wi-Fi not working in hostel block C, urgent ahead of online exam prep.",
        CasePriority.HIGH, "IT Helpdesk", CaseStatus.IN_PROGRESS, 1, 4, 24, None,
    ),
    CaseSeed(
        "CASE-0003", DEMO_STUDENT_CODE, "fees",
        "Fee receipt discrepancy is blocking exam hall ticket generation.",
        CasePriority.URGENT, "Accounts Office", CaseStatus.OPEN, 6, 2, 24, None,
    ),  # SLA-breached: resolution_due_at (created + 24h) is well in the past, unresolved.
    CaseSeed(
        "CASE-0004", "STU2023002", "hostel", "Broken chair in room A-101.",
        CasePriority.LOW, "Hostel Office", CaseStatus.RESOLVED, 10, 48, 168, 3,
    ),
    CaseSeed(
        "CASE-0005", "STU2023003", "facilities", "Library AC not cooling in reading hall.",
        CasePriority.NORMAL, "Facilities Office", CaseStatus.RESOLVED, 8, 24, 120, 4,
    ),
    CaseSeed(
        "CASE-0006", "STU2023004", "it_helpdesk", "Unable to access exam portal.",
        CasePriority.HIGH, "IT Helpdesk", CaseStatus.RESOLVED, 5, 4, 24, 1,
    ),
    CaseSeed(
        "CASE-0007", "STU2023005", "administrative", "Bonafide certificate request for visa application.",
        CasePriority.NORMAL, "Administrative Office", CaseStatus.CLOSED, 15, 24, 120, 10,
    ),
    CaseSeed(
        "CASE-0008", "STU2023007", "fees", "Scholarship disbursement not yet credited.",
        CasePriority.HIGH, "Accounts Office", CaseStatus.IN_PROGRESS, 3, 4, 24, None,
    ),
    CaseSeed(
        "CASE-0009", "STU2023009", "hostel", "Hostel room reallocation request.",
        CasePriority.LOW, "Hostel Office", CaseStatus.OPEN, 1, 48, 168, None,
    ),
    CaseSeed(
        "CASE-0010", "STU2023012", "facilities", "Projector not working in Block B - Room 201.",
        CasePriority.NORMAL, "Facilities Office", CaseStatus.OPEN, 2, 24, 120, None,
    ),
    CaseSeed(
        "CASE-0011", "STU2023014", "it_helpdesk", "Laptop charging point not working in Mech Lab.",
        CasePriority.NORMAL, "IT Helpdesk", CaseStatus.RESOLVED, 12, 24, 120, 7,
    ),
    CaseSeed(
        "CASE-0012", "STU2023018", "administrative", "Transcript request for higher studies application.",
        CasePriority.NORMAL, "Administrative Office", CaseStatus.OPEN, 4, 24, 120, None,
    ),
    CaseSeed(
        "CASE-0013", "STU2023020", "fees", "Fee installment plan request.",
        CasePriority.URGENT, "Accounts Office", CaseStatus.IN_PROGRESS, 7, 2, 24, None,
    ),  # also SLA-breached: resolution window has passed while still in progress.
]


def seed_cases(session: Session, students: dict[str, Student]) -> None:
    for spec in CASE_SEED:
        student = students[spec.student_code]
        created_at = SEED_NOW - timedelta(days=spec.created_days_ago)
        case, _ = get_or_create(
            session,
            CampusCase,
            case_code=spec.case_code,
            defaults={
                "student_id": student.id,
                "category": spec.category,
                "description": spec.description,
                "priority": spec.priority,
                "department": spec.department,
                "status": spec.status,
                "created_at": created_at,
                "updated_at": created_at,
            },
        )

        response_due_at = created_at + timedelta(hours=spec.response_hours)
        resolution_due_at = created_at + timedelta(hours=spec.resolution_hours)
        resolved_at = (
            created_at + timedelta(days=spec.resolved_days_ago) if spec.resolved_days_ago is not None else None
        )
        get_or_create(
            session,
            CaseSLA,
            case_id=case.id,
            defaults={
                "response_due_at": response_due_at,
                "resolution_due_at": resolution_due_at,
                "responded_at": created_at + timedelta(hours=1) if spec.status != CaseStatus.OPEN else None,
                "resolved_at": resolved_at,
            },
        )

        if spec.status in (CaseStatus.IN_PROGRESS, CaseStatus.RESOLVED, CaseStatus.CLOSED):
            get_or_create(
                session, CaseAssignment, case_id=case.id, assignee_name=f"{spec.department} Staff",
                defaults={"assigned_at": created_at + timedelta(hours=1)},
            )


# ---------------------------------------------------------------------------
# 8. Communication domain
# ---------------------------------------------------------------------------


def seed_communication(
    session: Session, students: dict[str, Student], events: dict[str, Event], courses: dict[str, Course]
) -> None:
    demo = students[DEMO_STUDENT_CODE]

    notification_specs = [
        (demo, "Attendance below threshold", "Your attendance in Operating Systems (CS301) is at 68%.", "academic"),
        (demo, "New internship posted", "AI Software Engineering Intern at NimbusCloud matches your profile.", "career"),
        (demo, "Event registration confirmed", "You're registered for the AI & Deep Learning Workshop.", "events"),
        (students["STU2023002"], "Application received", "Your application to Backend Engineer was received.", "career"),
        (students["STU2023007"], "SLA update", "Your scholarship disbursement case is being processed.", "services"),
        (students["STU2023009"], "Application accepted", "You've been accepted for the Embedded Systems Intern role.", "career"),
        (demo, "Case update", "Your fee receipt discrepancy case (CASE-0003) needs attention.", "services"),
        (students["STU2023018"], "Event reminder", "Startup Pitch Night is at capacity; you're confirmed.", "events"),
    ]
    for student, title, body, category in notification_specs:
        get_or_create(
            session, Notification, student_id=student.id, title=title, category=category,
            defaults={"body": body, "status": NotificationStatus.SENT, "sent_at": SEED_NOW},
        )

    ai_workshop = events["Artificial Intelligence & Deep Learning Workshop"]
    get_or_create(
        session, CalendarEvent, student_id=demo.id, title=ai_workshop.title, start_at=ai_workshop.start_at,
        defaults={"end_at": ai_workshop.end_at, "source_type": "event", "source_id": ai_workshop.id},
    )

    dbms_exam = session.execute(
        select(Exam).where(Exam.course_id == courses["CS302"].id, Exam.exam_type == "midterm")
    ).scalar_one_or_none()
    if dbms_exam is not None:
        get_or_create(
            session, CalendarEvent, student_id=demo.id, title="DBMS Midterm Exam",
            start_at=dbms_exam.scheduled_start,
            defaults={"end_at": dbms_exam.scheduled_end, "source_type": "exam", "source_id": dbms_exam.id},
        )

    pitch_night = events["Startup Pitch Night"]
    get_or_create(
        session, CalendarEvent, student_id=students["STU2023002"].id, title=pitch_night.title,
        start_at=pitch_night.start_at,
        defaults={"end_at": pitch_night.end_at, "source_type": "event", "source_id": pitch_night.id},
    )


# ---------------------------------------------------------------------------
# 9. Faculty, sections, teaching assignments (Phase 16)
# ---------------------------------------------------------------------------

# (employee_code, full_name, dept_code, designation, phone). Every course
# instructor in COURSE_SEED has a profile; Course.instructor stays as it was.
FACULTY_SEED: list[tuple[str, str, str, str, str]] = [
    ("EMP-CSE-001", "Dr. Kavita Iyer", "CSE", "Professor & Head of Department", "+91 80 4000 1001"),
    ("EMP-CSE-002", "Dr. Manoj Pillai", "CSE", "Associate Professor", "+91 80 4000 1002"),
    ("EMP-CSE-003", "Dr. Sunita Rao", "CSE", "Associate Professor", "+91 80 4000 1003"),
    ("EMP-CSE-004", "Dr. Ashok Verma", "CSE", "Assistant Professor", "+91 80 4000 1004"),
    ("EMP-CSE-005", "Dr. Leela Nair", "CSE", "Assistant Professor", "+91 80 4000 1005"),
    ("EMP-CSE-006", "Dr. Nikhil Bhatt", "CSE", "Assistant Professor", "+91 80 4000 1006"),
    ("EMP-ECE-001", "Dr. Ramesh Kumar", "ECE", "Associate Professor", "+91 80 4000 2001"),
    ("EMP-ECE-002", "Dr. Anjali Menon", "ECE", "Professor", "+91 80 4000 2002"),
    ("EMP-ECE-003", "Dr. Suresh Pillai", "ECE", "Assistant Professor", "+91 80 4000 2003"),
    ("EMP-MECH-001", "Dr. Ganesh Iyer", "MECH", "Associate Professor", "+91 80 4000 3001"),
    ("EMP-MECH-002", "Dr. Vinod Rao", "MECH", "Professor", "+91 80 4000 3002"),
    ("EMP-CIVIL-001", "Dr. Meenakshi Nair", "CIVIL", "Associate Professor", "+91 80 4000 4001"),
    ("EMP-CIVIL-002", "Dr. Prakash Reddy", "CIVIL", "Professor", "+91 80 4000 4002"),
]

DEFAULT_SECTION = "1"

# Phase 17: the faculty profile heading each department (departments.hod_faculty_id).
DEPARTMENT_HEADS = {"CSE": "EMP-CSE-001", "ECE": "EMP-ECE-002", "MECH": "EMP-MECH-002", "CIVIL": "EMP-CIVIL-002"}

# Mentor per (department, semester); CSE semester 5 section 1 (Aditi's
# class) is mentored by Dr. Ashok Verma.
MENTOR_PLAN: dict[tuple[str, int], str] = {
    ("CSE", 5): "EMP-CSE-004", ("CSE", 3): "EMP-CSE-001", ("CSE", 7): "EMP-CSE-006", ("CSE", 1): "EMP-CSE-001",
    ("ECE", 5): "EMP-ECE-002", ("ECE", 3): "EMP-ECE-001", ("ECE", 7): "EMP-ECE-002", ("ECE", 1): "EMP-ECE-001",
    ("MECH", 5): "EMP-MECH-002", ("MECH", 3): "EMP-MECH-001", ("MECH", 1): "EMP-MECH-001",
    ("CIVIL", 5): "EMP-CIVIL-002", ("CIVIL", 3): "EMP-CIVIL-001",
}

# More CSE semester-5 section-1 students, so a class roster is realistic.
# (student_code, full_name, cgpa, {course: (attended, conducted)})
CLASSMATE_SEED: list[tuple[str, str, float, dict[str, tuple[int, int]]]] = [
    ("STU2023021", "Nikhil Joshi", 7.4, {"CS303": (46, 50), "CS302": (45, 50)}),
    ("STU2023022", "Pooja Hegde", 8.2, {"CS303": (49, 50)}),
    ("STU2023023", "Farhan Ali", 6.6, {"CS303": (33, 50), "CS301": (35, 50)}),
    ("STU2023024", "Sneha Kulkarni", 7.9, {"CS303": (41, 50)}),
    ("STU2023025", "Harsh Vardhan", 6.9, {"CS303": (36, 50), "CS302": (34, 50)}),
    ("STU2023026", "Lakshmi Narayan", 8.7, {}),
    ("STU2023027", "Omkar Patil", 7.2, {"CS304": (35, 50)}),
    ("STU2023028", "Zoya Khan", 8.0, {}),
]
CLASSMATE_DEFAULT_ATTENDANCE = (44, 50)


def _faculty_email(full_name: str) -> str:
    return ".".join(full_name.replace("Dr. ", "").lower().split()) + "@meridian.edu"


def seed_faculty(session: Session, departments: dict[str, Department]) -> dict[str, FacultyProfile]:
    result: dict[str, FacultyProfile] = {}
    for code, full_name, dept_code, designation, phone in FACULTY_SEED:
        email = _faculty_email(full_name)
        user, _ = get_or_create(session, User, email=email, defaults={"full_name": full_name, "role": UserRole.FACULTY})
        profile, _ = get_or_create(
            session, FacultyProfile, employee_code=code,
            defaults={
                "user_id": user.id, "full_name": full_name, "department_id": departments[dept_code].id,
                "designation": designation, "email": email, "phone": phone,
            },
        )
        result[code] = profile
    return result


def seed_classmates(
    session: Session, departments: dict[str, Department], courses: dict[str, Course]
) -> dict[str, Student]:
    result: dict[str, Student] = {}
    for student_code, full_name, cgpa, attendance in CLASSMATE_SEED:
        user, _ = get_or_create(
            session, User, email=f"{student_code.lower()}@meridian.edu",
            defaults={"full_name": full_name, "role": UserRole.STUDENT},
        )
        student, _ = get_or_create(
            session, Student, student_code=student_code,
            defaults={
                "user_id": user.id, "department_id": departments["CSE"].id, "year": 3, "semester": 5, "cgpa": cgpa,
                "section": DEFAULT_SECTION,
            },
        )
        for course_code in DEPT_SEMESTER_COURSES["CSE"][5]:
            enrollment, _ = get_or_create(
                session, Enrollment, student_id=student.id, course_id=courses[course_code].id,
                academic_year=ACADEMIC_YEAR, defaults={"semester": 5},
            )
            attended, conducted = attendance.get(course_code, CLASSMATE_DEFAULT_ATTENDANCE)
            get_or_create(
                session, AttendanceRecord, enrollment_id=enrollment.id,
                defaults={"classes_attended": attended, "classes_conducted": conducted},
            )
        result[student_code] = student
    return result


def seed_teaching(session: Session, courses: dict[str, Course], faculty: dict[str, FacultyProfile]) -> None:
    by_name = {f.full_name: f for f in faculty.values()}
    for code, _title, dept_code, _credits, semester, instructor in COURSE_SEED:
        course = courses[code]
        get_or_create(
            session, TeachingAssignment, course_id=course.id, section=DEFAULT_SECTION, academic_term=ACADEMIC_YEAR,
            defaults={
                "faculty_id": by_name[instructor].id, "department_id": course.department_id,
                "year": (semester + 1) // 2, "semester": semester,
            },
        )
    for department in session.execute(select(Department)).scalars():
        head = DEPARTMENT_HEADS.get(department.code)
        if department.hod_faculty_id is None and head in faculty:
            department.hod_faculty_id = faculty[head].id
    # Sections and mentors are only filled in where still empty (idempotent,
    # never overwriting a later change).
    for student in session.execute(select(Student)).scalars():
        if student.section is None:
            student.section = DEFAULT_SECTION
        if student.mentor_faculty_id is None:
            mentor = MENTOR_PLAN.get((student.department.code, student.semester))
            if mentor in faculty:
                student.mentor_faculty_id = faculty[mentor].id
    session.flush()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class SeedSummary:
    departments: int
    students: int
    courses: int
    enrollments: int
    attendance_records: int
    timetable_slots: int
    exams: int
    companies: int
    skills: int
    opportunities: int
    student_skills: int
    applications: int
    clubs: int
    events: int
    event_registrations: int
    campus_cases: int
    case_slas: int
    notifications: int
    calendar_events: int
    faculty_profiles: int = 0
    teaching_assignments: int = 0


def _count(session: Session, model: type) -> int:
    return len(list(session.execute(select(model)).scalars().all()))


def build_summary(session: Session) -> SeedSummary:
    return SeedSummary(
        departments=_count(session, Department),
        students=_count(session, Student),
        courses=_count(session, Course),
        enrollments=_count(session, Enrollment),
        attendance_records=_count(session, AttendanceRecord),
        timetable_slots=_count(session, TimetableSlot),
        exams=_count(session, Exam),
        companies=_count(session, Company),
        skills=_count(session, Skill),
        opportunities=_count(session, Opportunity),
        student_skills=_count(session, StudentSkill),
        applications=_count(session, Application),
        clubs=_count(session, Club),
        events=_count(session, Event),
        event_registrations=_count(session, EventRegistration),
        campus_cases=_count(session, CampusCase),
        case_slas=_count(session, CaseSLA),
        notifications=_count(session, Notification),
        calendar_events=_count(session, CalendarEvent),
        faculty_profiles=_count(session, FacultyProfile),
        teaching_assignments=_count(session, TeachingAssignment),
    )


def run_seed(session: Session) -> SeedSummary:
    # Phase 22A: everything seeded belongs to the single demo organization.
    organization = ensure_default_organization(session)
    departments = seed_departments(session)
    students = seed_students(session, departments)
    courses = seed_courses(session, departments)
    seed_enrollments_and_academics(session, students, courses)
    seed_career(session, students, departments)
    clubs = seed_clubs(session)
    events = seed_events(session, clubs, students)
    seed_cases(session, students)
    seed_communication(session, students, events, courses)
    faculty = seed_faculty(session, departments)
    seed_classmates(session, departments, courses)
    seed_teaching(session, courses, faculty)
    assign_unowned_rows(session, organization)
    ensure_default_deployments(session, organization.id)  # the six catalog agents, deployed with defaults
    session.commit()
    return build_summary(session)


def main() -> None:
    engine = create_db_engine()
    init_db(engine)
    session_factory = create_session_factory(engine)
    password, password_source = resolve_seed_password(Path(__file__).resolve().parents[1] / "data" / "dev_credentials.txt")
    with session_factory() as session:
        summary = run_seed(session)
        created = seed_dev_accounts(session, password)

    print("Seed complete. Row counts:")
    for field_name, value in summary.__dict__.items():
        print(f"  {field_name}: {value}")
    print(f"Development sign-in accounts ({len(created)} new; password from {password_source}):")
    for spec in DEV_ACCOUNTS:
        print(f"  {spec.email} ({spec.role.value})")


if __name__ == "__main__":
    main()
