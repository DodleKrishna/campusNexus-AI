"""Deterministic CareerVerifier (CLAUDE.md component 8, scoped to the Career Agent).

Mirrors app/verification/academic.py: checks that the correct inputs were
used and that results are internally consistent, without re-implementing
`app.rules.opportunity_eligibility.compute_eligibility`'s full logic (that
would be a second copy to go stale independently). The one exception, as in
the Academic Verifier, is a cheap recomputation of the core invariant
(eligible iff no blocking reasons) as a genuine consistency check.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from app.schemas.career import (
    ApplicationSummary,
    CareerIntent,
    CareerProfile,
    OpportunityEligibility,
    OpportunityEligibilityStatus,
)
from app.schemas.enums import VerificationPhase, VerificationStatus
from app.schemas.evidence import Evidence
from app.schemas.verification import VerificationCheck, VerificationResult


@dataclass
class CareerVerificationInput:
    mission_id: str
    task_id: str
    intent: CareerIntent
    student: Optional[CareerProfile]
    requires_opportunities: bool
    requires_policy_evidence: bool
    eligibilities: List[OpportunityEligibility] = field(default_factory=list)
    applications: List[ApplicationSummary] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)


class CareerVerifier:
    def verify(
        self, data: CareerVerificationInput, *, phase: VerificationPhase = VerificationPhase.POST_ACTION
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

        if student_exists and data.intent == CareerIntent.UNKNOWN:
            checks.append(
                VerificationCheck(
                    name="intent_supported",
                    passed=False,
                    detail="Request could not be classified into a supported career operation.",
                )
            )
            failed = True
            issues.append("Unsupported career request.")

        if student_exists and data.requires_opportunities:
            has_data = bool(data.eligibilities)
            checks.append(
                VerificationCheck(
                    name="opportunities_present", passed=has_data, detail=None if has_data else "No opportunities were evaluated."
                )
            )
            if not has_data:
                needs_review = True
                issues.append("No open opportunities found to evaluate for eligibility.")
            else:
                consistent = all(
                    (e.status == OpportunityEligibilityStatus.ELIGIBLE) == (len(e.blocking_reasons) == 0)
                    for e in data.eligibilities
                )
                checks.append(
                    VerificationCheck(
                        name="eligibility_consistent",
                        passed=consistent,
                        detail=None if consistent else "An eligibility result's status does not match its blocking reasons.",
                    )
                )
                if not consistent:
                    failed = True
                    issues.append("Eligibility calculation failed an internal consistency check.")

        if student_exists and data.requires_policy_evidence:
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
