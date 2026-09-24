"""Deterministic failure fingerprints for bounded replanning (Phase 10).

A replan is only worth running if something could turn out differently. When
a task fails in exactly the same way as before -- same agent, same objective,
same target (constraints), same verification status, same normalized reasons,
and the same input context (the facts it was dispatched with) -- re-running it
cannot produce new information, so the Orchestrator stops instead of burning
another replan on it.

Pure functions, no I/O. The fingerprint deliberately excludes anything that
varies between attempts without meaning anything (task/mission ids, generated
``tc-``/``prop-``/``ver-`` identifiers), and deliberately *includes* the input
facts: if an upstream task produced different facts, the retry is meaningful
and is allowed.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Dict, Iterable, List

from app.schemas.common import JsonValue
from app.schemas.mission import MissionTask

# Generated identifiers (e.g. "tc-1a2b3c4d5e6f", "mission-0123456789ab") carry
# no information about *why* something failed.
_GENERATED_ID_RE = re.compile(r"\b(?:[a-z]+-)*[0-9a-f]{12}\b")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_reason(text: str, *, mission_id: str = "") -> str:
    normalized = text
    if mission_id:
        normalized = normalized.replace(mission_id, "<mission>")
    normalized = _GENERATED_ID_RE.sub("<id>", normalized.lower())
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def input_context_signature(input_facts: Dict[str, JsonValue]) -> str:
    """Hash of the facts a task was dispatched with (its own and its dependencies')."""
    return hashlib.sha256(_stable_json(input_facts).encode("utf-8")).hexdigest()[:16]


def failure_fingerprint(
    task: MissionTask,
    *,
    mission_id: str,
    verification_status: str,
    reasons: Iterable[str],
    input_facts: Dict[str, JsonValue],
) -> str:
    """A stable id for "this task failed this way, given this input"."""
    payload = {
        "agent": task.agent.value,
        "objective": normalize_reason(task.objective, mission_id=mission_id),
        "target": task.constraints,
        "verification_status": verification_status,
        "reasons": sorted(normalize_reason(r, mission_id=mission_id) for r in reasons if r),
        "input": input_context_signature(input_facts),
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()[:16]


def all_failures_repeated(failed_task_ids: List[str], repeated_task_ids: Iterable[str]) -> bool:
    """True when there is at least one failure and every one of them is a repeat."""
    repeated = set(repeated_task_ids)
    return bool(failed_task_ids) and all(task_id in repeated for task_id in failed_task_ids)
