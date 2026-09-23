"""Deterministic AcademicVerifier (CLAUDE.md component 8, scoped to the Academic Agent).

Per the Phase 4 spec, this checks that the *correct inputs/evidence/rules
were used* and that the result is *internally consistent* -- it deliberately
does not re-implement the attendance/eligibility math (that would just be a
second copy of app/rules/*.py to go stale independently). The one exception
is the core eligibility inequality, which is cheap and self-contained enough
to re-derive directly from the raw counters as a genuine consistency check
rather than trusting the rule module blindly.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from app.schemas.academic import (
    AcademicIntent,
    AttendanceCalculation,
    AttendanceRuleStatus,
    CourseResolution,
    CourseResolutionStatus,
    ExamEligibilityResult,
    ExamEligibilityStatus,
    PolicyThreshold,
    StudentAcademicProfile,
    ThresholdExtractionStatus,
)
from app.schemas.enums import VerificationPhase, VerificationStatus
from app.schemas.evidence import Evidence
from app.schemas.verification import VerificationCheck, VerificationResult


@dataclass
class AcademicVerificationInput:
    """Everything the AcademicVerifier needs, plus which checks apply to this intent.

    ``requires_course``/``requires_attendance``/``requires_policy_evidence``
    are set by the caller (the Academic Agent, which knows what it actually
    attempted for this intent) rather than re-derived here from ``intent``,
    so the check logic below stays a flat set of "if required and missing/bad"
    rules instead of duplicating intent-branching that already lives in the agent.
    """

    mission_id: str
    task_id: str
    intent: AcademicIntent
    student: Optional[StudentAcademicProfile]
    requires_course: bool
    requires_attendance: bool
    requires_policy_evidence: bool
    course_resolution: Optional[CourseResolution] = None
    attendance: Optional[AttendanceCalculation] = None
    threshold: Optional[PolicyThreshold] = None
    eligibility: Optional[ExamEligibilityResult] = None
    evidence: List[Evidence] = field(default_factory=list)


class AcademicVerifier:
    """Deterministic pre/post-condition checks for one Academic Agent handling."""

    def verify(
        self, data: AcademicVerificationInput, *, phase: VerificationPhase = VerificationPhase.PRE_ACTION
    ) -> VerificationResult:
        checks: List[VerificationCheck] = []
        issues: List[str] = []
        failed = False
        needs_review = False

        student_exists = data.student is not None
        checks.append(
            VerificationCheck(
                name="student_exists", passed=student_exists, detail=None if student_exists else "No student record found."
            )
        )
        if not student_exists:
            failed = True
            issues.append("Student record not found.")

        if student_exists and data.intent == AcademicIntent.UNKNOWN:
            checks.append(
                VerificationCheck(
                    name="intent_supported",
                    passed=False,
                    detail="Request could not be classified into a supported academic operation.",
                )
            )
            failed = True
            issues.append("Unsupported academic request.")

        if student_exists and data.requires_course:
            failed, needs_review = self._check_course(data, checks, issues, failed, needs_review)

        # If a course was required but not cleanly RESOLVED (AMBIGUOUS/UNKNOWN),
        # the agent never fetched attendance/evidence for it -- that's a direct
        # consequence of the course check above, not an independent data
        # problem, so downstream checks are skipped rather than compounding
        # into a spurious FAILED on top of the course's own NEEDS_REVIEW/FAILED.
        course_blocked = data.requires_course and (
            data.course_resolution is None or data.course_resolution.status != CourseResolutionStatus.RESOLVED
        )
        # Likewise, if attendance requires a policy threshold and the threshold
        # extraction didn't cleanly succeed, the agent never computes
        # attendance for it -- that's a direct consequence of the threshold
        # check below, not a second independent failure.
        threshold_blocked = (
            data.requires_attendance
            and data.requires_policy_evidence
            and (data.threshold is None or data.threshold.status != ThresholdExtractionStatus.OK)
        )

        if student_exists and data.requires_attendance and not course_blocked and not threshold_blocked:
            failed, needs_review = self._check_attendance_source(data, checks, issues, failed, needs_review)

        if student_exists and data.requires_policy_evidence and not course_blocked:
            failed, needs_review = self._check_evidence(data, checks, issues, failed, needs_review)

        failed, needs_review = self._check_rule_consistency(data, checks, issues, failed, needs_review)

        if failed:
            status = VerificationStatus.FAILED
        elif needs_review:
            status = VerificationStatus.NEEDS_REVIEW
        else:
            status = VerificationStatus.VERIFIED

        return VerificationResult(
            verification_id=f"ver-{uuid.uuid4().hex[:12]}",
            mission_id=data.mission_id,
            task_id=data.task_id,
            phase=phase,
            status=status,
            checks=checks,
            issues=issues,
            evidence_refs=[e.evidence_id for e in data.evidence],
        )

    def _check_course(self, data, checks, issues, failed, needs_review):
        cr = data.course_resolution
        resolved = cr is not None and cr.status == CourseResolutionStatus.RESOLVED
        checks.append(
            VerificationCheck(
                name="course_resolved",
                passed=resolved,
                detail=None if resolved else f"Course resolution status: {cr.status.value if cr else 'missing'}",
            )
        )
        if cr is not None and cr.status == CourseResolutionStatus.UNKNOWN:
            failed = True
            issues.append("Referenced course does not match any enrolled course.")
        elif cr is not None and cr.status == CourseResolutionStatus.AMBIGUOUS:
            needs_review = True
            issues.append("Referenced course is ambiguous among enrolled courses.")
        elif cr is None or cr.status == CourseResolutionStatus.NOT_REQUESTED:
            failed = True
            issues.append("A course reference was required but none was resolved.")
        return failed, needs_review

    def _check_attendance_source(self, data, checks, issues, failed, needs_review):
        att = data.attendance
        source_ok = att is not None and att.status != AttendanceRuleStatus.INVALID_INPUT
        checks.append(
            VerificationCheck(
                name="attendance_source_exists",
                passed=source_ok,
                detail=None if source_ok else "Attendance source data missing or invalid.",
            )
        )
        if att is None:
            failed = True
            issues.append("Attendance data was required but not computed.")
        elif att.status == AttendanceRuleStatus.INVALID_INPUT:
            failed = True
            issues.append("Attendance source data is invalid.")
        elif att.status == AttendanceRuleStatus.NO_CLASSES_CONDUCTED:
            needs_review = True
            issues.append("No classes have been conducted yet for this course.")
        return failed, needs_review

    def _check_evidence(self, data, checks, issues, failed, needs_review):
        evidence_present = bool(data.evidence)
        checks.append(
            VerificationCheck(
                name="evidence_present", passed=evidence_present, detail=None if evidence_present else "No policy evidence retrieved."
            )
        )
        if not evidence_present:
            needs_review = True
            issues.append("Insufficient policy evidence to ground this answer.")

        if data.requires_attendance:
            threshold_status = data.threshold.status if data.threshold else None
            threshold_ok = threshold_status == ThresholdExtractionStatus.OK
            checks.append(
                VerificationCheck(
                    name="threshold_unambiguous",
                    passed=threshold_ok,
                    detail=None if threshold_ok else f"Threshold extraction status: {threshold_status.value if threshold_status else 'missing'}",
                )
            )
            if threshold_status == ThresholdExtractionStatus.AMBIGUOUS:
                needs_review = True
                issues.append("Active attendance policy evidence gives conflicting thresholds.")
            elif threshold_status == ThresholdExtractionStatus.NOT_FOUND:
                needs_review = True
                issues.append("Could not deterministically extract an attendance threshold from policy evidence.")
            elif threshold_status is None:
                failed = True
                issues.append("Threshold extraction was required but not attempted.")
        return failed, needs_review

    def _check_rule_consistency(self, data, checks, issues, failed, needs_review):
        att = data.attendance
        if att is not None and att.status == AttendanceRuleStatus.OK:
            exact_ratio = Decimal(att.classes_attended) / Decimal(att.classes_conducted)
            expected_eligible = exact_ratio >= (att.required_percentage / Decimal(100))
            consistent = expected_eligible == att.eligible_now
            checks.append(
                VerificationCheck(
                    name="rule_consistent",
                    passed=consistent,
                    detail=None if consistent else "Attendance rule output is internally inconsistent.",
                )
            )
            if not consistent:
                failed = True
                issues.append("Attendance calculation failed an internal consistency check.")

        elig = data.eligibility
        if elig is not None and att is not None:
            expected_status = ExamEligibilityStatus.UNKNOWN
            if att.status == AttendanceRuleStatus.OK and att.eligible_now is not None:
                expected_status = (
                    ExamEligibilityStatus.ELIGIBLE if att.eligible_now else ExamEligibilityStatus.NOT_ELIGIBLE
                )
            consistent = elig.status == expected_status
            checks.append(
                VerificationCheck(
                    name="eligibility_consistent",
                    passed=consistent,
                    detail=None if consistent else "Exam eligibility result does not match the attendance calculation.",
                )
            )
            if not consistent:
                failed = True
                issues.append("Exam eligibility result is internally inconsistent with attendance data.")
            elif elig.status == ExamEligibilityStatus.UNKNOWN:
                needs_review = True
                issues.append("Exam eligibility could not be determined from attendance data.")

        return failed, needs_review
