"""Deterministic EventsVerifier (CLAUDE.md component 8, scoped to the Events & Opportunity Agent).

Mirrors app/verification/academic.py. Availability and conflict detection
are separate, both-tracked dimensions (an event can be AVAILABLE to
register and still have a timetable/exam conflict) -- this verifier checks
that they weren't conflated, not that one implies the other.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from app.schemas.enums import VerificationPhase, VerificationStatus
from app.schemas.events import EventAssessment, EventsIntent
from app.schemas.evidence import Evidence
from app.schemas.verification import VerificationCheck, VerificationResult


@dataclass
class EventsVerificationInput:
    mission_id: str
    task_id: str
    intent: EventsIntent
    student_exists: bool
    requires_events: bool
    requires_policy_evidence: bool
    expected_conflict_check: bool = False
    assessments: List[EventAssessment] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)


class EventsVerifier:
    def verify(
        self, data: EventsVerificationInput, *, phase: VerificationPhase = VerificationPhase.POST_ACTION
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

        if data.student_exists and data.intent == EventsIntent.UNKNOWN:
            checks.append(
                VerificationCheck(
                    name="intent_supported",
                    passed=False,
                    detail="Request could not be classified into a supported events operation.",
                )
            )
            failed = True
            issues.append("Unsupported events request.")

        if data.student_exists and data.requires_events:
            has_data = bool(data.assessments)
            checks.append(
                VerificationCheck(
                    name="events_present", passed=has_data, detail=None if has_data else "No relevant events were found."
                )
            )
            if not has_data:
                needs_review = True
                issues.append("No relevant upcoming events found.")

            if data.expected_conflict_check:
                conflict_checks_ran = all(a.conflict_check_performed for a in data.assessments)
                checks.append(
                    VerificationCheck(
                        name="conflict_check_performed",
                        passed=conflict_checks_ran,
                        detail=None if conflict_checks_ran else "Timetable/exam data was expected but conflict checking was skipped.",
                    )
                )
                if not conflict_checks_ran:
                    needs_review = True
                    issues.append("Could not confirm schedule-conflict checking used the expected timetable/exam data.")

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
