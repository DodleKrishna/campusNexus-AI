"""Deterministic ActionVerifier (CLAUDE.md component 8, scoped to the Action Agent).

The first real use of ``VerificationPhase.PRE_ACTION``/``POST_ACTION``
(reserved since Phase 1, unused until now). Unlike the read-only agents'
verifiers, this one doesn't recompute domain facts itself -- the named
``VerificationCheck`` list is built by ``app/rules/action_preconditions.py``
(pre-action) or by direct independent DB re-reads in
``app/agents/action/agent.py`` (post-action); this module's only job is to
turn that check list into the right ``VerificationResult`` status.

A handful of checks are "soft": failing them means the outcome is ambiguous
(couldn't confirm a schedule-conflict check ran, or a conflict/overlap was
found) rather than structurally wrong, so they downgrade the result to
NEEDS_REVIEW instead of FAILED -- the action still requires human approval
either way (every write tool in this phase requires approval regardless),
but a FAILED precheck never even reaches approval, per CLAUDE.md's "never
accept an unverified recommendation as authorization to execute".
"""
from __future__ import annotations

import uuid
from typing import FrozenSet, List, Optional

from app.schemas.enums import VerificationPhase, VerificationStatus
from app.schemas.evidence import Evidence
from app.schemas.verification import VerificationCheck, VerificationResult

SOFT_CHECK_NAMES = {"schedule_conflict_checked", "no_schedule_conflicts", "no_schedule_overlap"}


class ActionVerifier:
    def verify_pre_action(
        self,
        *,
        mission_id: str,
        task_id: str,
        checks: List[VerificationCheck],
        evidence: Optional[List[Evidence]] = None,
        blocking_check_names: FrozenSet[str] = frozenset(),
    ) -> VerificationResult:
        """``blocking_check_names`` escalates the named (normally soft) checks to
        hard failures for this call -- Phase 10: a schedule conflict that is
        *known* before approval blocks the proposal instead of reaching a human."""
        return self._build(mission_id, task_id, VerificationPhase.PRE_ACTION, checks, evidence or [], blocking_check_names)

    def verify_post_action(
        self, *, mission_id: str, task_id: str, checks: List[VerificationCheck]
    ) -> VerificationResult:
        return self._build(mission_id, task_id, VerificationPhase.POST_ACTION, checks, [])

    @staticmethod
    def _build(
        mission_id: str,
        task_id: str,
        phase: VerificationPhase,
        checks: List[VerificationCheck],
        evidence: List[Evidence],
        blocking_check_names: FrozenSet[str] = frozenset(),
    ) -> VerificationResult:
        soft_names = SOFT_CHECK_NAMES - blocking_check_names
        issues = [c.detail for c in checks if not c.passed and c.detail]
        hard_failed = any(not c.passed for c in checks if c.name not in soft_names)
        soft_failed = any(not c.passed for c in checks if c.name in soft_names)

        if hard_failed:
            status = VerificationStatus.FAILED
        elif soft_failed:
            status = VerificationStatus.NEEDS_REVIEW
        else:
            status = VerificationStatus.VERIFIED

        return VerificationResult(
            verification_id=f"ver-{uuid.uuid4().hex[:12]}",
            mission_id=mission_id,
            task_id=task_id,
            phase=phase,
            status=status,
            checks=checks,
            issues=issues,
            evidence_refs=[e.evidence_id for e in evidence],
        )
