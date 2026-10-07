"""Redaction and size bounds for everything the kernel persists or shows an AgentBrain.

Applied to mission context, step summaries, observations, domain-event payloads
and audit metadata. Key-based: any field whose name suggests a secret,
credential or phone number is replaced, and phone-number-shaped strings are
masked wherever they appear. Output is plain JSON (dict/list/str/int/float/bool/None).
"""
from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "[redacted]"
_SENSITIVE_KEY = re.compile(
    r"pass(word|wd)?|secret|token|api[_-]?key|credential|authori[sz]ation|cookie|session[_-]?id|private[_-]?key"
    r"|(^|_)(otp|pin)($|_)|phone|mobile|msisdn|reasoning|thought|scratchpad",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?<![\w:.+-])\+?\d[\d\s().-]{8,}\d(?![\w:])")
MAX_DEPTH, MAX_ITEMS, MAX_STRING = 6, 50, 1000
MAX_SUMMARY_CHARS = 8000


def _mask_phone(match: "re.Match[str]") -> str:
    digits = sum(ch.isdigit() for ch in match.group())
    return REDACTED if 10 <= digits <= 15 else match.group()


def _is_sensitive(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def redact(value: Any, _depth: int = 0) -> Any:
    """A JSON-safe, redacted, size-bounded copy of ``value``."""
    if _depth > MAX_DEPTH:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = _PHONE.sub(_mask_phone, value)
        return text if len(text) <= MAX_STRING else text[:MAX_STRING] + "...[truncated]"
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                out["_truncated"] = True
                break
            key = str(key)[:80]
            out[key] = REDACTED if _is_sensitive(key) else redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item, _depth + 1) for item in list(value)[:MAX_ITEMS]]
    if hasattr(value, "model_dump"):
        return redact(value.model_dump(mode="json"), _depth)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return redact(str(value), _depth)


def bounded(value: Any) -> Any:
    """``redact`` plus a hard cap on the serialized size of one persisted summary."""
    safe = redact(value)
    if len(json.dumps(safe, default=str)) > MAX_SUMMARY_CHARS:
        return {"_truncated": True}
    return safe
