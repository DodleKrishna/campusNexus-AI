"""Deterministic attendance-threshold extraction from policy evidence.

CLAUDE.md is explicit that an LLM must never be the final authority for
turning policy prose into an official number. This module is a validated
parser tied to the known structure of the attendance policy corpus: it only
looks inside the ``"Minimum Attendance Requirement"`` section of retrieved
``Evidence`` and extracts a ``NN%`` figure with a regex, never asking an LLM
to interpret the text. If that section is absent, or if active evidence
disagrees on the number, the result is ``NOT_FOUND``/``AMBIGUOUS`` rather
than a silent fallback -- callers must not compute eligibility in that case
(see app/verification/academic.py).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from app.schemas.academic import PolicyThreshold, ThresholdCandidate, ThresholdExtractionStatus
from app.schemas.evidence import Evidence

_THRESHOLD_SECTION = "minimum attendance requirement"

# Primary pattern matches the corpus's actual phrasing ("... minimum of 75%
# attendance ..."); the fallback covers any other "<number>% ... attendance"
# phrasing within the same known section without falling back to a generic
# "any percentage in this text" guess.
_PRIMARY_PATTERN = re.compile(r"minimum of\s+(\d{1,3}(?:\.\d+)?)\s*%", re.IGNORECASE)
_FALLBACK_PATTERN = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:minimum\s+)?attendance", re.IGNORECASE)


def _extract_percentage(text: str) -> Optional[Decimal]:
    match = _PRIMARY_PATTERN.search(text) or _FALLBACK_PATTERN.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def extract_attendance_threshold(evidence: List[Evidence]) -> PolicyThreshold:
    """Extract the active attendance threshold from a list of policy Evidence.

    Expects ``evidence`` to already be filtered to the currently-active
    policy window (e.g. via ``KnowledgeService.get_active_policy`` with an
    ``as_of`` date) -- this function does not do date filtering itself, only
    structural extraction + ambiguity detection.
    """
    candidates: List[ThresholdCandidate] = []
    matches: List[tuple[Evidence, Decimal]] = []

    for item in evidence:
        if item.section is None or _THRESHOLD_SECTION not in item.section.strip().lower():
            continue
        value = _extract_percentage(item.snippet)
        if value is None:
            continue
        matches.append((item, value))
        candidates.append(
            ThresholdCandidate(
                document_id=item.document_id,
                policy_version=item.policy_version,
                required_percentage=value,
            )
        )

    if not matches:
        return PolicyThreshold(status=ThresholdExtractionStatus.NOT_FOUND, candidates=candidates)

    distinct_values = {value for _, value in matches}
    if len(distinct_values) > 1:
        return PolicyThreshold(status=ThresholdExtractionStatus.AMBIGUOUS, candidates=candidates)

    evidence_item, value = matches[0]
    return PolicyThreshold(
        status=ThresholdExtractionStatus.OK,
        required_percentage=value,
        document_id=evidence_item.document_id,
        policy_version=evidence_item.policy_version,
        section=evidence_item.section,
        effective_from=evidence_item.effective_from.isoformat() if evidence_item.effective_from else None,
        evidence_id=evidence_item.evidence_id,
        candidates=candidates,
    )
