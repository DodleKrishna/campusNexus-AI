"""Academic Agent vertical-slice schemas (Phase 4).

Three families of models live here, all Pydantic per CLAUDE.md's Structured
Output Requirement:

- Service DTOs (``StudentAcademicProfile``, ``CourseSummary``,
  ``AttendanceSnapshot``, ``TimetableEntry``, ``ExamEntry``): typed, read-only
  data returned by ``app/services/academic.py``.
- Rule outputs (``AttendanceCalculation``, ``PolicyThreshold``,
  ``ExamEligibilityResult``): produced by the plain-Python functions in
  ``app/rules/*.py``, never by an LLM call.
- LLM/agent boundary models (``AcademicIntent``, ``AcademicIntentResult``,
  ``CourseResolution``, ``AcademicResponseContext``): the structured contract
  between the Academic Agent, its LLM provider, and the AcademicVerifier.
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.common import NonBlankStr
from app.schemas.enums import VerificationStatus
from app.schemas.evidence import Evidence

# ---------------------------------------------------------------------------
# Academic intent (LLM classification boundary)
# ---------------------------------------------------------------------------


class AcademicIntent(str, Enum):
    """The supported categories of academic request the LLM may classify a query into."""

    ATTENDANCE_STATUS = "attendance_status"
    ATTENDANCE_RECOVERY = "attendance_recovery"
    EXAM_ELIGIBILITY = "exam_eligibility"
    TIMETABLE = "timetable"
    EXAM_SCHEDULE = "exam_schedule"
    POLICY_QUESTION = "policy_question"
    UNKNOWN = "unknown"


class AcademicIntentResult(BaseModel):
    """Structured output of ``LLMProvider.classify_academic_intent``.

    ``raw_course_reference`` is whatever course-like substring the LLM
    noticed in the query (e.g. "OS", "CS301") -- it is a proposal only.
    ``app.agents.academic.course_resolution.resolve_course`` is what actually
    resolves it against the student's real enrolled courses; the LLM never
    gets to assert a course exists.
    """

    intent: AcademicIntent
    raw_course_reference: Optional[str] = None


# ---------------------------------------------------------------------------
# Service DTOs (app/services/academic.py)
# ---------------------------------------------------------------------------


class CourseSummary(BaseModel):
    """A course the student is enrolled in, typed and read-only."""

    course_code: NonBlankStr
    title: NonBlankStr
    credits: int
    semester: int
    instructor: NonBlankStr


class StudentAcademicProfile(BaseModel):
    """A compact, typed snapshot of a student's academic identity + enrollments."""

    student_code: NonBlankStr
    full_name: NonBlankStr
    department_code: NonBlankStr
    year: int
    semester: int
    cgpa: float
    courses: List[CourseSummary] = Field(default_factory=list)


class AttendanceSnapshot(BaseModel):
    """Raw attendance counters for one enrollment -- never a stored percentage."""

    course_code: NonBlankStr
    course_title: NonBlankStr
    classes_attended: int
    classes_conducted: int


class TimetableEntry(BaseModel):
    """One recurring weekly class slot."""

    course_code: NonBlankStr
    course_title: NonBlankStr
    weekday: int = Field(ge=0, le=6)
    start_time: str
    end_time: str
    location: NonBlankStr


class ExamEntry(BaseModel):
    """One scheduled exam."""

    course_code: NonBlankStr
    course_title: NonBlankStr
    exam_type: NonBlankStr
    scheduled_start: str
    scheduled_end: str
    location: NonBlankStr


# ---------------------------------------------------------------------------
# Course resolution (app/agents/academic/course_resolution.py)
# ---------------------------------------------------------------------------


class CourseResolutionStatus(str, Enum):
    """Outcome of resolving an LLM-proposed course reference against enrollments."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"
    NOT_REQUESTED = "not_requested"


class CourseResolution(BaseModel):
    """Deterministic course-reference resolution result.

    ``candidates`` is only populated when AMBIGUOUS (the course codes that
    matched) so a clarifying response can name them.
    """

    status: CourseResolutionStatus
    course_code: Optional[str] = None
    candidates: List[str] = Field(default_factory=list)
    query_reference: Optional[str] = None


# ---------------------------------------------------------------------------
# Attendance rule (app/rules/attendance.py)
# ---------------------------------------------------------------------------


class AttendanceRuleStatus(str, Enum):
    """Outcome of the deterministic attendance calculation."""

    OK = "ok"
    NO_CLASSES_CONDUCTED = "no_classes_conducted"
    INVALID_INPUT = "invalid_input"


class AttendanceCalculation(BaseModel):
    """Deterministic result of ``compute_attendance`` -- Decimal throughout, no floats.

    ``current_percentage`` is rounded to 1 decimal place (matching the
    policy's own stated calculation method) and ``eligible_now`` is evaluated
    against that rounded value, so "exactly at threshold" is unambiguous.
    """

    status: AttendanceRuleStatus
    classes_attended: int
    classes_conducted: int
    required_percentage: Decimal
    current_percentage: Optional[Decimal] = None
    eligible_now: Optional[bool] = None
    classes_needed_to_reach_threshold: Optional[int] = None
    threshold_reachable: Optional[bool] = None
    maximum_additional_absences_allowed: Optional[int] = None
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Policy threshold extraction (app/rules/policy_threshold.py)
# ---------------------------------------------------------------------------


class ThresholdExtractionStatus(str, Enum):
    """Outcome of extracting a numeric attendance threshold from policy evidence."""

    OK = "ok"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


class ThresholdCandidate(BaseModel):
    """One (document, value) pair considered during ambiguous extraction, for diagnostics."""

    document_id: NonBlankStr
    policy_version: Optional[str] = None
    required_percentage: Decimal


class PolicyThreshold(BaseModel):
    """Deterministic, traceable result of ``extract_attendance_threshold``.

    A ``status=OK`` result is always traceable to a single document_id /
    policy_version / section / effective_from -- never a silent fallback.
    """

    status: ThresholdExtractionStatus
    required_percentage: Optional[Decimal] = None
    document_id: Optional[str] = None
    policy_version: Optional[str] = None
    section: Optional[str] = None
    effective_from: Optional[str] = None
    evidence_id: Optional[str] = None
    candidates: List[ThresholdCandidate] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Exam eligibility (app/rules/eligibility.py)
# ---------------------------------------------------------------------------


class ExamEligibilityStatus(str, Enum):
    """Outcome of the (attendance-only) exam eligibility rule."""

    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"
    UNKNOWN = "unknown"


class ExamEligibilityResult(BaseModel):
    """Attendance-based exam eligibility.

    Fee-clearance is part of the official exam eligibility rule per
    exam_regulations.md, but fee data belongs to the (out-of-scope in this
    phase) Campus Services Agent -- ``caveat`` says so explicitly rather than
    the result silently overclaiming full eligibility.
    """

    status: ExamEligibilityStatus
    based_on: NonBlankStr = "attendance_only"
    attendance: AttendanceCalculation
    caveat: str = (
        "Fee-clearance status is not evaluated by this system; contact the "
        "Accounts Office to confirm there is no fee hold before relying on this."
    )


# ---------------------------------------------------------------------------
# Final response generation (app/llm/base.py)
# ---------------------------------------------------------------------------


class AcademicResponseContext(BaseModel):
    """Everything ``LLMProvider.generate_academic_response`` may read.

    Deliberately assembled only from already-verified structured data --
    the renderer (mock or real LLM) must not fetch or invent anything beyond
    what's in this object (CLAUDE.md Structured Output Requirement: free text
    only at the final user-facing boundary, and only grounded in verified facts).
    """

    intent: AcademicIntent
    verification_status: VerificationStatus
    verification_issues: List[str] = Field(default_factory=list)
    student_name: Optional[str] = None
    course: Optional[CourseSummary] = None
    course_resolution: Optional[CourseResolution] = None
    attendance: Optional[AttendanceCalculation] = None
    eligibility: Optional[ExamEligibilityResult] = None
    threshold: Optional[PolicyThreshold] = None
    timetable: List[TimetableEntry] = Field(default_factory=list)
    exams: List[ExamEntry] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
