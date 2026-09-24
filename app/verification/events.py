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
    # Phase 12B: whether a relevance filter was applied (every returned event
    # must then carry the terms it matched), the upstream Career skill gaps it
    # was matched against (None = no Career dependency), and whether those
    # upstream gaps arrived in an unusable shape.
    relevance_filter_active: bool = False
    skill_gaps: Optional[List[str]] = None
    skill_gaps_malformed: bool = False


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

        if data.skill_gaps_malformed:
            checks.append(
                VerificationCheck(
                    name="skill_gaps_well_formed",
                    passed=False,
                    detail="Upstream skill gaps were not a list of skill names.",
                )
            )
            failed = True
            issues.append("Upstream skill gaps were malformed, so events could not be matched against them.")

        if data.student_exists and data.requires_events and not data.skill_gaps_malformed:
            has_data = bool(data.assessments)
            checks.append(
                VerificationCheck(
                    name="events_present", passed=has_data, detail=None if has_data else "No relevant events were found."
                )
            )
            if not has_data:
                needs_review = True
                if data.skill_gaps is None:
                    issues.append("No relevant upcoming events found.")
                elif data.skill_gaps:
                    issues.append(f"No upcoming event matches the skill gaps: {', '.join(data.skill_gaps)}.")
                else:
                    issues.append("No skill gaps were identified upstream, so no event could be matched to one.")

            if data.relevance_filter_active:
                untraceable = [a.event.title for a in data.assessments if not a.matched_terms]
                checks.append(
                    VerificationCheck(
                        name="relevance_traceable",
                        passed=not untraceable,
                        detail=None if not untraceable else f"Returned without a matching term: {', '.join(untraceable)}.",
                    )
                )
                if untraceable:
                    failed = True
                    issues.append("An event was presented as relevant without any matching term.")

            if data.skill_gaps is not None:
                unknown_gaps = sorted({g for a in data.assessments for g in a.matched_skill_gaps} - set(data.skill_gaps))
                checks.append(
                    VerificationCheck(
                        name="skill_gap_matches_consistent",
                        passed=not unknown_gaps,
                        detail=None if not unknown_gaps else f"Matched gaps not in the upstream list: {', '.join(unknown_gaps)}.",
                    )
                )
                if unknown_gaps:
                    failed = True
                    issues.append("An event claims to address a skill gap that was not identified upstream.")

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
