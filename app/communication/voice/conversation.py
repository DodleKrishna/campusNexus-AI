"""Turn-based voice conversation (AgentOS V2 Phase 5): the pipeline behind the Exotel WebSocket stream.

One ``VoiceConversation`` per stream, bound by a verified stream token to one organization and one attempt. Each
turn: the recipient's audio -> ``SpeechToText`` (text kept in memory for this turn only) -> ``VoiceTurnBrain``, which
returns a structured ``VoiceTurn`` (a reply *key* from a fixed list and, optionally, an outcome code from
``VoiceOutcome``) -> the reply text comes from fixed templates -> ``TextToSpeech``. The brain never writes what is
said and never sees a name or contact detail; an invalid turn ends the call safely.

Bounded by code: at most ``max_turns`` turns and ``voice_time_limit_seconds``; the first breach ends the call
(NO_RESPONSE). Persisted: the VoiceSession's status, turn count, outcome code and timestamps -- never audio, the
transcript or the brain's input. The outcome is applied through ``callbacks.apply_outcome`` (a response never
changes an attendance, submission or exam record).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.communication.connectors import compose_message
from app.communication.sources import SourceFacts, SourceUnavailable, adapter_for
from app.communication.voice import tokens
from app.communication.voice.callbacks import apply_outcome
from app.db.models.communication_delivery import (
    AttemptStatus, CommunicationAttempt, CommunicationJob, VoiceSession, VoiceSessionStatus,
)
from app.db.tenant_session import TenantSessionFactory
from app.rules.communication_policy import CommunicationPolicy, VoiceOutcome

ReplyKey = Literal["acknowledge", "offer_help", "callback_noted", "dispute_noted", "repeat", "goodbye"]
REPLIES = {
    "acknowledge": "Thank you. I have noted that.",
    "offer_help": "I will let your faculty member know that you need help. They will contact you.",
    "callback_noted": "I will ask your faculty member to call you back.",
    "dispute_noted": "I have noted that you think this is not correct. Your faculty member will review it.",
    "repeat": "Sorry, I did not catch that. Could you say it again?",
    "goodbye": "Thank you. Goodbye.",
}


class VoiceTurn(BaseModel):
    """The brain's structured decision for one turn. Free text is impossible: replies and outcomes are allowlisted."""

    model_config = ConfigDict(extra="forbid")

    reply: ReplyKey
    outcome: Optional[VoiceOutcome] = None
    end_call: bool = False


@dataclass(frozen=True)
class VoiceTurnContext:
    purpose: str
    facts: SourceFacts  # safe facts only
    turn: int
    max_turns: int
    utterance: str  # this turn's recognized speech; in memory only, never persisted


class SpeechToText(Protocol):
    def transcribe(self, audio: bytes) -> str: ...


class TextToSpeech(Protocol):
    def synthesize(self, text: str) -> bytes: ...


class VoiceTurnBrain(Protocol):
    def next_turn(self, context: VoiceTurnContext) -> Any: ...


@dataclass(frozen=True)
class VoicePipeline:
    stt: SpeechToText
    tts: TextToSpeech
    brain: VoiceTurnBrain


class ConversationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VoiceConversation:
    def __init__(self, factory: Any, setup: Any, pipeline: VoicePipeline, token: str, clock: Callable[[], datetime],
                 policy: Optional[CommunicationPolicy] = None) -> None:
        self.factory = TenantSessionFactory.from_sessionmaker(factory)
        self.setup, self.pipeline, self.clock = setup, pipeline, clock
        self.policy = policy or setup.policy
        try:
            self.claims = tokens.verify(token, "stream", clock())
        except tokens.VoiceTokenError as exc:
            raise ConversationError(f"TOKEN_{exc}") from None
        self.ended = False
        self._purpose, self._facts, self._started = "", None, clock()

    def _session(self):
        return self.factory.open_tenant_session(self.claims.organization_id)

    def _load(self, session: Any) -> tuple:
        attempt = session.get(CommunicationAttempt, self.claims.attempt_id)
        voice = session.execute(select(VoiceSession).where(
            VoiceSession.communication_attempt_id == self.claims.attempt_id)).scalars().first()
        if attempt is None or voice is None or attempt.status in (AttemptStatus.DELIVERED, AttemptStatus.FAILED) \
                or voice.status in (VoiceSessionStatus.COMPLETED, VoiceSessionStatus.FAILED):
            raise ConversationError("SESSION_NOT_ACTIVE")
        return attempt, voice, session.get(CommunicationJob, attempt.job_id)

    def start(self, stream_reference: Optional[str]) -> bytes:
        """The stream connected: mark the session CONNECTED and return the opening message's audio."""
        now = self.clock()
        with self._session() as session:
            attempt, voice, job = self._load(session)
            try:
                adapter = adapter_for(self.setup.adapters, job.source_type)
                self._facts = adapter.facts(session, adapter.load(session, job.source_followup_id))
            except SourceUnavailable as exc:
                raise ConversationError(exc.code) from None
            self._purpose, self._started = job.purpose, now
            voice.status, voice.connected_at, voice.updated_at = VoiceSessionStatus.CONNECTED, now, now
            voice.stream_reference = (stream_reference or "")[:80] or None
            session.commit()
        _, body = compose_message(self._purpose, self._facts)
        return self.pipeline.tts.synthesize(body)

    def on_utterance(self, audio: bytes) -> Optional[bytes]:
        """One recipient turn -> the reply audio, or None once the call has ended."""
        if self.ended:
            return None
        now = self.clock()
        with self._session() as session:
            try:
                _, voice, _ = self._load(session)
            except ConversationError:  # the provider already settled the call (hang-up): nothing more to say
                return self._finish(None, None, "CALL_ENDED")
            voice.turn_count += 1
            voice.updated_at = now
            over = voice.turn_count > voice.max_turns or (now - self._started).total_seconds() >= \
                self.policy.voice_time_limit_seconds
            session.commit()
            turn_number, max_turns = voice.turn_count, voice.max_turns
        if over:
            return self._finish(VoiceOutcome.NO_RESPONSE.value, "goodbye", "LIMIT_REACHED")
        context = VoiceTurnContext(self._purpose, self._facts, turn_number, max_turns,
                                   self.pipeline.stt.transcribe(audio)[:500])
        try:
            raw = self.pipeline.brain.next_turn(context)
            turn = raw if isinstance(raw, VoiceTurn) else VoiceTurn.model_validate(raw)
        except Exception:  # noqa: BLE001 -- an invalid or failed turn ends the call safely
            return self._finish(VoiceOutcome.OTHER_SAFE.value, "goodbye", "INVALID_TURN")
        if turn.end_call or turn_number >= max_turns:
            outcome = turn.outcome.value if turn.outcome else VoiceOutcome.OTHER_SAFE.value
            return self._finish(outcome, turn.reply if turn.end_call else "goodbye", "COMPLETED")
        if turn.outcome is not None:
            self._record_outcome(turn.outcome.value)
        return self.pipeline.tts.synthesize(REPLIES[turn.reply])

    def stop(self) -> None:
        """The stream closed (hang-up). Without an outcome the call ends as NO_RESPONSE."""
        if not self.ended:
            self._finish(None, None, "STREAM_STOPPED")

    def _record_outcome(self, outcome: str) -> None:
        with self._session() as session:
            _, voice, _ = self._load(session)
            voice.outcome_code = outcome
            session.commit()

    def _finish(self, outcome: Optional[str], reply: Optional[str], ended_by: str) -> Optional[bytes]:
        """Record the conversation's result once. Works whether the provider's final callback came before or after
        the stream ended: the outcome is applied to the job by whichever side sees it second, exactly once (an
        attempt's ``outcome_code`` records which outcome the callback already applied)."""
        self.ended = True
        now = self.clock()
        with self._session() as session:
            attempt = session.get(CommunicationAttempt, self.claims.attempt_id)
            voice = session.execute(select(VoiceSession).where(
                VoiceSession.communication_attempt_id == self.claims.attempt_id)).scalars().first()
            if attempt is None or voice is None or voice.outcome is not None:
                return None
            voice.outcome_code = outcome or voice.outcome_code or VoiceOutcome.NO_RESPONSE.value
            voice.outcome = {"outcome": voice.outcome_code, "turns": voice.turn_count, "ended_by": ended_by}
            voice.ended_at, voice.updated_at = voice.ended_at or now, now
            if attempt.status == AttemptStatus.DELIVERED and attempt.outcome_code != voice.outcome_code:
                attempt.outcome_code = voice.outcome_code  # settled before the stream ended: apply it now
                apply_outcome(session, session.get(CommunicationJob, attempt.job_id), voice.outcome_code, now)
            session.commit()
        return self.pipeline.tts.synthesize(REPLIES[reply]) if reply else None
