"""CAMPUS AI grounded answers: the structured data that crosses the service / LLM boundary.

``GroundedPrompt`` is the *whole* context an LLM may see for one answer: a compact instruction, the caller's
role, the question, a few deterministic facts, at most four retrieved chunks and a short recent-conversation
window. ``GroundedSynthesis`` is the only shape an answer may come back in; the service validates it again
(cited ids, numbers, required names) before it is shown.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

NOT_FOUND_ANSWER = "I couldn't find that in the campus records."
MAX_FACTS = 12
MAX_CHUNKS = 4
MAX_HISTORY_TURNS = 3
MAX_ANSWER_CHARS = 1200


class GroundedFact(BaseModel):
    """One deterministic fact from the database (or a deterministic rule result)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^F\d{1,2}$")
    label: str = Field(max_length=80)
    value: Any


class GroundedSource(BaseModel):
    """One retrieved institutional-knowledge chunk (cited by ``id``)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^S\d$")
    source_id: str
    title: str
    section: str
    text: str = Field(max_length=700)


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: str = Field(max_length=300)
    assistant: str = Field(default="", max_length=300)


class GroundedPrompt(BaseModel):
    """Everything the synthesis model receives. Built only by ``app.services.grounded_answers``."""

    model_config = ConfigDict(extra="forbid")

    intent: str
    caller_role: str
    caller_name: Optional[str] = None
    question: str = Field(max_length=500)
    facts: List[GroundedFact] = Field(default_factory=list, max_length=MAX_FACTS)
    sources: List[GroundedSource] = Field(default_factory=list, max_length=MAX_CHUNKS)
    recent_conversation: List[ConversationTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)
    draft_answer: str = Field(max_length=MAX_ANSWER_CHARS * 2)

    def payload(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class GroundedSynthesis(BaseModel):
    """The model's answer. ``used_fact_ids`` / ``used_source_ids`` must name only ids it was given."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    used_fact_ids: List[str] = Field(default_factory=list, max_length=MAX_FACTS)
    used_source_ids: List[str] = Field(default_factory=list, max_length=MAX_CHUNKS)


class Citation(BaseModel):
    source_id: str
    title: str
    section: str


class GroundedAnswer(BaseModel):
    """What a caller (the Nexus tool) gets back."""

    intent: str
    found: bool
    answer: str
    citations: List[Citation] = Field(default_factory=list)
    fact_count: int = 0
    answered_by: str  # "deterministic" or the model id that actually answered
    provider: Optional[str] = None
    synthesis_status: str  # not_attempted | accepted | rejected:<code> | unavailable:<code>
    context_tokens_estimate: int = 0
