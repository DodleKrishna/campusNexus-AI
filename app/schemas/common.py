"""Shared type aliases and validation helpers used across app.schemas modules.

Not one of the explicitly listed Phase 1 schema files, but factored out to avoid
duplicating identical blank-string validation and JSON-value typing in every
schema module. Contains no business logic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, JsonValue

__all__ = ["utc_now", "NonBlankStr", "JsonValue"]


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _reject_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


NonBlankStr = Annotated[str, AfterValidator(_reject_blank)]
"""A string field that rejects empty/whitespace-only values (mission/task/etc. ids)."""

# Re-exported from pydantic: a recursive JSON-compatible value, used instead of
# ``Any`` for free-form payloads (tool arguments/results, agent facts/constraints,
# audit metadata). Pydantic's own implementation avoids the recursion-depth issues
# a hand-rolled `Union[..., List["JsonValue"], Dict[str, "JsonValue"]]` alias hits.
