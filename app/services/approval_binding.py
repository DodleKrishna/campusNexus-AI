"""Approval payload binding (Phase 11) -- part of the Approval Gate (component 9).

A human approval authorizes one exact, validated action -- never "whatever
this mission step turns out to execute later". This module builds the
canonical *approved payload* for a proposed write and a deterministic
fingerprint of it. The fingerprint is stored on the ``ApprovalRecord`` when
the approval is requested and recomputed from the ``ToolCallRecord`` right
before execution; any difference blocks execution.

Fields that contribute to the fingerprint (and nothing else):

* ``binding_version``   -- this module's schema version (``BINDING_VERSION``)
* ``mission_id``        -- the mission the approval belongs to
* ``step_id``           -- the mission step (task) the approval belongs to
* ``tool_name``         -- the write tool to be invoked
* ``actor_student_id``  -- the student the action is performed for/as
* ``target_resource``   -- the resource the write targets (``target_resource_for``)
* ``arguments``         -- the tool arguments, normalized through the tool's
  own ``input_model`` (Pydantic validate + JSON dump) when available, so
  equivalent spellings of the same value hash identically
* ``precheck_status``   -- the deterministic pre-approval verdict the human
  saw when approving

Deliberately excluded: timestamps, generated ids (approval/tool-call/proposal
ids), idempotency keys, human-readable summaries, evidence snippets. They vary
without changing *what the action does*.

Serialization is canonical JSON (sorted keys, fixed separators, ASCII) and the
hash is SHA-256 -- never Python's process-randomized built-in ``hash()``, so a
fingerprint computed in one process matches the one recomputed in another.
Pure functions, no I/O.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional, Type

from pydantic import BaseModel

BINDING_VERSION = 1

# The fields that make up an approved payload, in documentation order.
FINGERPRINT_FIELDS = (
    "binding_version",
    "mission_id",
    "step_id",
    "tool_name",
    "actor_student_id",
    "target_resource",
    "arguments",
    "precheck_status",
)


def target_resource_for(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """The resource a write targets, derived only from its arguments -- so the
    execution gate can re-derive it instead of trusting a stored string."""
    student_id = arguments.get("student_id")
    if tool_name == "register_event":
        return f"event:{arguments.get('event_id')}"
    if tool_name == "create_calendar_event":
        return f"calendar:{student_id}:{arguments.get('title')}"
    if tool_name == "create_campus_case":
        return f"case:{student_id}:{arguments.get('category')}"
    return f"{tool_name}:{student_id}"


def normalize_arguments(arguments: Mapping[str, Any], input_model: Optional[Type[BaseModel]] = None) -> Dict[str, Any]:
    """Canonical JSON-safe form of tool arguments.

    With the tool's ``input_model`` the arguments are validated and dumped
    exactly as the Tool Gateway would see them; if they do not validate, the
    raw values are kept (the gateway rejects them at execution anyway, and a
    fingerprint must never raise)."""
    if input_model is not None:
        try:
            return input_model.model_validate(dict(arguments)).model_dump(mode="json")
        except ValueError:
            pass
    return json.loads(json.dumps(dict(arguments), default=str))


def build_approval_payload(
    *,
    mission_id: str,
    step_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    precheck_status: str,
    input_model: Optional[Type[BaseModel]] = None,
) -> Dict[str, Any]:
    """The canonical logical action an approval authorizes."""
    normalized = normalize_arguments(arguments, input_model)
    return {
        "binding_version": BINDING_VERSION,
        "mission_id": mission_id,
        "step_id": step_id,
        "tool_name": tool_name,
        "actor_student_id": str(normalized.get("student_id") or ""),
        "target_resource": target_resource_for(tool_name, normalized),
        "arguments": normalized,
        "precheck_status": precheck_status,
    }


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def approval_fingerprint(payload: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical JSON of exactly ``FINGERPRINT_FIELDS``."""
    missing = [name for name in FINGERPRINT_FIELDS if name not in payload]
    if missing:
        raise ValueError(f"approval payload is missing fingerprint field(s): {missing}")
    bound = {name: payload[name] for name in FINGERPRINT_FIELDS}
    return hashlib.sha256(canonical_json(bound).encode("utf-8")).hexdigest()
