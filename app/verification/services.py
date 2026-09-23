"""Deterministic ServicesVerifier (CLAUDE.md component 8, scoped to the Campus Services Agent).

Mirrors app/verification/academic.py. "Case ownership" is a genuine
re-check here (not the Phase 5 self-dependency situation, where the Phase 1
schema already made the bad state unconstructable) -- the read-only
repository's join-filtered query *should* already guarantee it, but a query
bug is exactly the kind of thing type validation can't catch, so this
re-derives it from the fetched data instead of trusting the query blindly.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from app.rules.sla import compute_sla_breaches
from app.schemas.enums import VerificationPhase, VerificationStatus
from app.schemas.evidence import Evidence
from app.schemas.services import CaseSLAAssessment, ServicesIntent
from app.schemas.verification import VerificationCheck, VerificationResult


@dataclass
class ServicesVerificationInput:
    mission_id: str
    task_id: str
    intent: ServicesIntent
    student_exists: bool
    expected_student_code: Optional[str]
    requires_cases: bool
    requires_policy_evidence: bool
    now: datetime
    case_assessments: List[CaseSLAAssessment] = field(default_factory=list)
    cases_missing_sla: List[str] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)


class ServicesVerifier:
    def verify(
        self, data: ServicesVerificationInput, *, phase: VerificationPhase = VerificationPhase.POST_ACTION
    ) -> VerificationResult:
        checks: List[VerificationCheck] = []
        issues: List[str] = []
        failed = False
        needs_review = False

        checks.append(
            VerificationCheck(
                name="student_exists", passed=data.student_exists, detail=None if data.student_exists else "No student record found."
            )
        )
        if not data.student_exists:
            failed = True
            issues.append("Student record not found.")

        if data.student_exists and data.intent == ServicesIntent.UNKNOWN:
            checks.append(
                VerificationCheck(
                    name="intent_supported",
                    passed=False,
                    detail="Request could not be classified into a supported campus services operation.",
                )
            )
            failed = True
            issues.append("Unsupported campus services request.")

        if data.student_exists and data.requires_cases:
            has_data = bool(data.case_assessments)
            checks.append(
                VerificationCheck(
                    name="cases_present", passed=has_data, detail=None if has_data else "No cases were found for this student."
                )
            )
            if not has_data and not data.cases_missing_sla:
                needs_review = True
                issues.append("No cases found for this student.")

            if has_data:
                owned = all(a.case.student_code == data.expected_student_code for a in data.case_assessments)
                checks.append(
                    VerificationCheck(
                        name="case_ownership",
                        passed=owned,
                        detail=None if owned else "A returned case does not belong to the requesting student.",
                    )
                )
                if not owned:
                    failed = True
                    issues.append("Case ownership check failed -- a returned case belongs to a different student.")

                consistent = True
                for assessment in data.case_assessments:
                    expected_response, expected_resolution = compute_sla_breaches(
                        response_due_at=assessment.response_due_at,
                        resolution_due_at=assessment.resolution_due_at,
                        responded_at=assessment.responded_at,
                        resolved_at=assessment.resolved_at,
                        now=data.now,
                    )
                    if expected_response != assessment.response_breached or expected_resolution != assessment.resolution_breached:
                        consistent = False
                        break
                checks.append(
                    VerificationCheck(
                        name="sla_consistent",
                        passed=consistent,
                        detail=None if consistent else "An SLA breach flag does not match a recomputation from its due dates.",
                    )
                )
                if not consistent:
                    failed = True
                    issues.append("SLA calculation failed an internal consistency check.")

            if data.cases_missing_sla:
                checks.append(
                    VerificationCheck(
                        name="sla_data_present",
                        passed=False,
                        detail=f"No SLA record found for case(s): {', '.join(data.cases_missing_sla)}",
                    )
                )
                needs_review = True
                issues.append(f"SLA data missing for case(s): {', '.join(data.cases_missing_sla)}")

        if data.student_exists and data.requires_policy_evidence:
            evidence_present = bool(data.evidence)
            checks.append(
                VerificationCheck(
                    name="evidence_present", passed=evidence_present, detail=None if evidence_present else "No policy evidence retrieved."
                )
            )
            if not evidence_present:
                needs_review = True
                issues.append("Insufficient policy evidence to ground this answer.")

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
