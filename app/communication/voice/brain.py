"""The restricted VoiceConversationBrain (AgentOS V2 Phase 5.1).

An outbound call is never connected to Nexus or any tool. The brain sees only ``VoiceBrainInput``: the purpose, the
source's safe facts (kind, title, course code, time), the turn number, the last few short exchanges of *this* call
(in memory only) and the allowed outcome codes. It must answer with ``VoiceBrainReply`` -- a short reply (<= 180
characters, no URLs, no e-mail addresses), one outcome code from ``VoiceOutcome`` and whether to continue -- and
nothing else (``extra="forbid"``; the Groq request uses a strict JSON schema with no tools). Anything that fails
validation is a ``SpeechProviderError`` and the call ends with a fixed sentence.

What the student says never changes a record: an outcome code is only reported to the Guardian and, for help,
disputes and callbacks, flagged for staff review.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.communication.voice.speech import GroqHTTP, SpeechProviderError
from app.rules.communication_policy import VoiceOutcome

MAX_REPLY_CHARS = 180
MAX_HISTORY = 8
_FORBIDDEN = re.compile(r"https?://|www\.|\b[\w.-]+\.(com|in|org|net|edu|io)\b|@|<|>|\{|\}", re.IGNORECASE)
_PHONE_LIKE = re.compile(r"\d[\d\s-]{6,}\d")


class VoiceExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: Literal["student", "assistant"]
    text: str = Field(max_length=200)


class VoiceBrainInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    kind: str  # assignment | exam | class
    title: str
    course_code: str | None
    when: str
    turn: int
    max_turns: int
    history: List[VoiceExchange] = Field(default_factory=list, max_length=MAX_HISTORY)
    allowed_outcomes: List[str] = Field(default_factory=lambda: [o.value for o in VoiceOutcome])


class VoiceBrainReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reply: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)
    outcome_code: VoiceOutcome
    continue_conversation: bool

    @field_validator("reply")
    @classmethod
    def _safe(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value or _FORBIDDEN.search(value) or _PHONE_LIKE.search(value):
            raise ValueError("unsafe reply")
        return value


def parse_reply(raw: Any) -> VoiceBrainReply:
    """Strict validation of anything a brain returned. Raises ``SpeechProviderError('BRAIN_INVALID_OUTPUT')``."""
    try:
        if isinstance(raw, VoiceBrainReply):
            return VoiceBrainReply.model_validate(raw.model_dump(mode="python"))
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict) and isinstance(raw.get("outcome_code"), str):
            raw = {**raw, "outcome_code": VoiceOutcome(raw["outcome_code"])}  # strict mode: enum from its value
        return VoiceBrainReply.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        raise SpeechProviderError("BRAIN_INVALID_OUTPUT") from None


class VoiceConversationBrain(Protocol):
    def decide(self, context: VoiceBrainInput) -> Any: ...


SYSTEM_PROMPT = (
    "You are CampusNexus, an institution's automated phone assistant, on a short call with a student about ONE matter "
    "(described in the user message as JSON). Rules: speak only about that matter; reply in one or two short spoken "
    "sentences (at most 180 characters); never give URLs, e-mail addresses, phone numbers or any personal data; you "
    "cannot change, confirm or look up any record and must never claim that anything was done, except that a "
    "faculty member will be informed when the student needs help, disputes the record or asks for a callback; if the "
    "student says the record is wrong, say it will be reviewed (outcome DISPUTES_STATUS). Choose exactly one "
    "outcome_code from allowed_outcomes describing the student's answer so far, and set continue_conversation to "
    "false once the answer is clear or the student wants to end the call."
)

_SCHEMA: Dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["reply", "outcome_code", "continue_conversation"],
    "properties": {
        "reply": {"type": "string", "maxLength": MAX_REPLY_CHARS},
        "outcome_code": {"type": "string", "enum": [o.value for o in VoiceOutcome]},
        "continue_conversation": {"type": "boolean"},
    },
}


class GroqVoiceBrain:
    """One strict-JSON chat completion per turn (no tools, hidden reasoning, small output budget)."""

    def __init__(self, http: GroqHTTP, model: str) -> None:
        self.http, self.model = http, model

    def decide(self, context: VoiceBrainInput) -> VoiceBrainReply:
        body = {
            "model": self.model, "temperature": 0, "max_completion_tokens": 400, "reasoning_effort": "low",
            "include_reasoning": False,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": context.model_dump_json()}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "voice_turn", "strict": True, "schema": _SCHEMA}},
        }
        response = self.http.post("/chat/completions", json=body)
        try:
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") not in (None, "stop"):
                raise SpeechProviderError("BRAIN_TRUNCATED")
            if choice["message"].get("tool_calls"):
                raise SpeechProviderError("BRAIN_INVALID_OUTPUT")
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise SpeechProviderError("BRAIN_BAD_RESPONSE") from None
        return parse_reply(content)
