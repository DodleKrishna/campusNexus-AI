"""Live, turn-based voice conversation (AgentOS V2 Phase 5.1): the pipeline behind the Exotel WebSocket stream.

Per call: a deterministic opening (``opening_text`` from the source's safe facts; no LLM) -> then, per utterance from
the VAD: ``SpeechToTextProvider`` -> ``VoiceConversationBrain`` (restricted, strict schema) -> ``TextToSpeechProvider``
-> a ``VoiceTurnResult`` with the reply PCM. Turn-based, not full-duplex: the stream pauses listening while a reply
plays (``app.api.routers.voice``).

Only ``CallState`` holds conversational text, in memory, for the lifetime of one WebSocket; it is never persisted.
``VoiceConversation`` (bound by a verified stream token to one organization and one attempt) persists only the
VoiceSession's status, turn count, outcome code and a small ``outcome`` summary (counts, latencies, how it ended).

Failure behaviour (bounded, never pretending an action succeeded):
* STT failure -> one short "please repeat" (``stt_retries``), then the call ends (NO_RESPONSE);
* empty / low-confidence speech -> does not advance the turn; at most ``max_reprompts`` reprompts;
* brain failure or invalid output -> a fixed sentence and the call ends (OTHER_SAFE);
* TTS failure -> the call ends without audio; the attempt fails as VOICE_PIPELINE_FAILED (retried by policy);
* a disconnect -> the session closes; the provider's callback / the timeout sweep still decide the attempt.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from sqlalchemy import select, update

from app.communication.sources import SourceFacts, SourceUnavailable, adapter_for
from app.communication.voice import tokens
from app.communication.voice.brain import (
    MAX_HISTORY, VoiceBrainInput, VoiceConversationBrain, VoiceExchange, parse_reply,
)
from app.communication.voice.callbacks import apply_outcome
from app.communication.voice.brain import GroqVoiceBrain
from app.communication.voice.speech import (
    GroqHTTP, GroqSpeechToText, GroqTextToSpeech, SpeechProviderError, SpeechToTextProvider, TextToSpeechProvider,
    VoiceProviderConfig,
)
from app.communication.voice.vad import UtteranceDetector, VadConfig, VoiceActivity
from app.communication.worker import record_failed_attempt
from app.db.models.communication_delivery import (
    AttemptStatus, CommunicationAttempt, CommunicationJob, CommunicationJobStatus, VoiceSession, VoiceSessionStatus,
)
from app.db.tenant_session import TenantSessionFactory
from app.rules.communication_policy import CommunicationPolicy, VoiceOutcome

PIPELINE_FAILED = "VOICE_PIPELINE_FAILED"
SAY = {
    "reprompt": "Sorry, I did not catch that. Could you please say it again?",
    "goodbye": "Thank you for your time. Goodbye.",
    "stt_failed": "Sorry, I am having trouble hearing you. We will try again later. Goodbye.",
    "brain_failed": "Sorry, I cannot continue this call right now. Your faculty member may contact you. Goodbye.",
}
_ASK = {
    "submission_reminder": "Will you be able to submit it on time?",
    "deadline_warning": "Will you be able to submit it before the deadline?",
    "exam_reminder": "Are you aware of it?",
    "absence_followup": "You were marked absent. Do you need help with the next steps?",
    "absence_check": "You were recorded absent. Is that correct?",
}
MAX_OPENING_CHARS = 200
MAX_VOICE_TITLE = 40


def opening_text(purpose: str, facts: SourceFacts) -> str:
    """The deterministic first sentence (no LLM). Safe facts only; short enough for one TTS request."""
    title = facts.title if len(facts.title) <= MAX_VOICE_TITLE else facts.title[:MAX_VOICE_TITLE - 3].rstrip() + "..."
    if facts.kind == "class":
        subject = f"your {facts.course_code or ''} class on {facts.when_text()}".replace("  ", " ")
    else:
        course = f" for {facts.course_code}" if facts.course_code else ""
        timing = "due" if facts.kind == "assignment" else "on"
        subject = f"your {facts.kind} {title}{course}, {timing} {facts.when_text()}"
    text = f"Hello. This is CampusNexus calling about {subject}. {_ASK.get(purpose, '')}".strip()
    return text[:MAX_OPENING_CHARS]


@dataclass
class CallState:
    """In memory, one WebSocket call only. The history is the only place spoken text ever lives."""

    purpose: str
    facts: SourceFacts
    max_turns: int
    turn: int = 0
    reprompts: int = 0
    stt_failures: int = 0
    outcome: Optional[str] = None
    history: Deque[VoiceExchange] = field(default_factory=lambda: deque(maxlen=MAX_HISTORY))
    latency_ms: Dict[str, int] = field(default_factory=lambda: {"stt": 0, "brain": 0, "tts": 0})

    def __repr__(self) -> str:  # never print the history
        return f"CallState(turn={self.turn}, reprompts={self.reprompts}, outcome={self.outcome})"


@dataclass(frozen=True)
class VoiceTurnResult:
    status: str  # ANSWERED | REPROMPT | COMPLETED | MAX_TURNS | NO_RESPONSE | STT_FAILED | BRAIN_FAILED | TTS_FAILED
    reply_pcm: Optional[bytes]
    end_call: bool
    outcome_code: Optional[str] = None

    def __repr__(self) -> str:
        return f"VoiceTurnResult(status={self.status}, end_call={self.end_call}, outcome={self.outcome_code})"


class VoicePipeline:
    def __init__(self, stt: SpeechToTextProvider, tts: TextToSpeechProvider, brain: VoiceConversationBrain, *,
                 vad_config: Optional[VadConfig] = None, vad_factory: Optional[Callable[[], VoiceActivity]] = None,
                 max_reprompts: int = 2, stt_retries: int = 1, timer: Callable[[], float] = time.perf_counter) -> None:
        self.stt, self.tts, self.brain = stt, tts, brain
        self.vad_config, self.vad_factory = vad_config or VadConfig(), vad_factory
        self.max_reprompts, self.stt_retries, self.timer = max_reprompts, stt_retries, timer

    def new_detector(self) -> UtteranceDetector:
        return UtteranceDetector(self.vad_config, self.vad_factory() if self.vad_factory else None)

    def _timed(self, state: CallState, key: str, call: Callable[[], Any]) -> Any:
        started = self.timer()
        try:
            return call()
        finally:
            state.latency_ms[key] += int((self.timer() - started) * 1000)

    def say(self, state: CallState, text: str) -> bytes:
        """Raises ``SpeechProviderError`` (TTS failed)."""
        return self._timed(state, "tts", lambda: self.tts.synthesize(text))

    def opening(self, state: CallState) -> bytes:
        return self.say(state, opening_text(state.purpose, state.facts))

    def end(self, state: CallState, status: str, text: str, outcome: str) -> VoiceTurnResult:
        state.outcome = state.outcome or outcome
        try:
            pcm = self.say(state, text)
        except SpeechProviderError:
            return VoiceTurnResult("TTS_FAILED", None, True, state.outcome)
        return VoiceTurnResult(status, pcm, True, state.outcome)

    def on_utterance(self, state: CallState, pcm: bytes) -> VoiceTurnResult:
        try:
            transcript = self._timed(state, "stt", lambda: self.stt.transcribe(pcm))
        except SpeechProviderError:
            state.stt_failures += 1
            if state.stt_failures > self.stt_retries:
                return self.end(state, "STT_FAILED", SAY["stt_failed"], VoiceOutcome.NO_RESPONSE.value)
            return self._reprompt(state)
        if not transcript.usable:  # silence / noise / a guess: never advances the conversation
            state.reprompts += 1
            if state.reprompts > self.max_reprompts:
                return self.end(state, "NO_RESPONSE", SAY["goodbye"], VoiceOutcome.NO_RESPONSE.value)
            return self._reprompt(state)

        state.turn += 1
        context = VoiceBrainInput(purpose=state.purpose, kind=state.facts.kind, title=state.facts.title[:120],
                                  course_code=state.facts.course_code, when=state.facts.when_text(), turn=state.turn,
                                  max_turns=state.max_turns,
                                  history=[*state.history, VoiceExchange(speaker="student", text=transcript.text[:200])])
        try:
            decision = parse_reply(self._timed(state, "brain", lambda: self.brain.decide(context)))
        except Exception:  # noqa: BLE001 -- provider error, invalid output, anything: a fixed sentence ends the call
            return self.end(state, "BRAIN_FAILED", SAY["brain_failed"], VoiceOutcome.OTHER_SAFE.value)
        state.history.append(VoiceExchange(speaker="student", text=transcript.text[:200]))
        state.history.append(VoiceExchange(speaker="assistant", text=decision.reply))
        state.outcome = decision.outcome_code.value
        last = state.turn >= state.max_turns
        end = not decision.continue_conversation or last
        text = decision.reply
        if last and decision.continue_conversation:
            text = f"{decision.reply} Goodbye."[:200]
        try:
            reply = self.say(state, text)
        except SpeechProviderError:
            return VoiceTurnResult("TTS_FAILED", None, True, state.outcome)
        status = "MAX_TURNS" if last and decision.continue_conversation else ("COMPLETED" if end else "ANSWERED")
        return VoiceTurnResult(status, reply, end, state.outcome)

    def _reprompt(self, state: CallState) -> VoiceTurnResult:
        try:
            return VoiceTurnResult("REPROMPT", self.say(state, SAY["reprompt"]), False, state.outcome)
        except SpeechProviderError:
            return VoiceTurnResult("TTS_FAILED", None, True, state.outcome)


class ConversationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VoiceConversation:
    """One stream. The token maps to exactly one attempt; ``start`` claims its VoiceSession PENDING -> CONNECTED with
    one conditional UPDATE, so a replayed or duplicate stream can never open a second live session."""

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
        self.state: Optional[CallState] = None
        self._started = clock()

    def _session(self) -> Any:
        return self.factory.open_tenant_session(self.claims.organization_id)

    def _rows(self, session: Any) -> tuple:
        attempt = session.get(CommunicationAttempt, self.claims.attempt_id)  # tenant-filtered
        voice = session.execute(select(VoiceSession).where(
            VoiceSession.communication_attempt_id == self.claims.attempt_id)).scalars().first()
        return attempt, voice

    def start(self, stream_reference: Optional[str]) -> bytes:
        """Claim the session and return the opening's PCM."""
        now = self.clock()
        with self._session() as session:
            attempt, voice = self._rows(session)
            if attempt is None or voice is None or attempt.status in (AttemptStatus.DELIVERED, AttemptStatus.FAILED):
                raise ConversationError("SESSION_NOT_ACTIVE")
            claimed = session.execute(update(VoiceSession).where(
                VoiceSession.id == voice.id, VoiceSession.status == VoiceSessionStatus.PENDING,
            ).values(status=VoiceSessionStatus.CONNECTED, connected_at=now, updated_at=now,
                     stream_reference=(stream_reference or "")[:80] or None).execution_options(synchronize_session=False))
            if claimed.rowcount != 1:
                session.rollback()
                raise ConversationError("STREAM_ALREADY_ACTIVE")
            job = session.get(CommunicationJob, attempt.job_id)
            try:
                adapter = adapter_for(self.setup.adapters, job.source_type)
                facts = adapter.facts(session, adapter.load(session, job.source_followup_id))
            except SourceUnavailable as exc:
                session.rollback()
                raise ConversationError(exc.code) from None
            session.commit()
        self._started = now
        self.state = CallState(job.purpose, facts, max_turns=voice.max_turns)
        try:
            return self.pipeline.opening(self.state)
        except SpeechProviderError:
            self._pipeline_failed("TTS_FAILED")
            raise ConversationError("TTS_FAILED") from None

    def on_utterance(self, pcm: bytes) -> VoiceTurnResult:
        if self.ended or self.state is None:
            return VoiceTurnResult("ENDED", None, True, None)
        with self._session() as session:
            attempt, voice = self._rows(session)
            settled = attempt is None or attempt.status == AttemptStatus.FAILED or voice is None \
                or voice.status != VoiceSessionStatus.CONNECTED
        if settled:  # the provider already ended the call (hang-up) or the attempt failed
            self._finish("CALL_ENDED")
            return VoiceTurnResult("ENDED", None, True, self.state.outcome)
        if (self.clock() - self._started).total_seconds() >= self.policy.voice_time_limit_seconds:
            result = self.pipeline.end(self.state, "TIME_LIMIT", SAY["goodbye"], VoiceOutcome.NO_RESPONSE.value)
        else:
            result = self.pipeline.on_utterance(self.state, pcm)
        self._persist_progress()
        if result.status == "TTS_FAILED":
            self._pipeline_failed("TTS_FAILED")
        elif result.end_call:
            self._finish(result.status)
        return result

    def stop(self) -> None:
        """The stream closed. The provider's final callback (or the timeout sweep) still settles the attempt."""
        if not self.ended:
            self._finish("STREAM_STOPPED")

    def _summary(self, ended_by: str) -> Dict[str, Any]:
        state = self.state
        return {"outcome": state.outcome if state else None, "turns": state.turn if state else 0,
                "reprompts": state.reprompts if state else 0, "ended_by": ended_by,
                "latency_ms": dict(state.latency_ms) if state else {}}

    def _persist_progress(self) -> None:
        with self._session() as session:
            _, voice = self._rows(session)
            if voice is not None and self.state is not None:
                voice.turn_count, voice.updated_at = self.state.turn, self.clock()
                if self.state.outcome:
                    voice.outcome_code = self.state.outcome
                session.commit()

    def _finish(self, ended_by: str) -> None:
        """Record the conversation's result once. The outcome reaches the job exactly once, whichever arrives second:
        this, or the provider's final callback (an attempt's ``outcome_code`` records what the callback applied)."""
        self.ended = True
        now = self.clock()
        with self._session() as session:
            attempt, voice = self._rows(session)
            if attempt is None or voice is None or voice.outcome is not None:
                return
            outcome = (self.state.outcome if self.state else None) or voice.outcome_code or VoiceOutcome.NO_RESPONSE.value
            voice.outcome_code = outcome
            voice.turn_count = self.state.turn if self.state else voice.turn_count
            voice.outcome = {**self._summary(ended_by), "outcome": outcome}
            voice.ended_at, voice.updated_at = voice.ended_at or now, now
            if attempt.status == AttemptStatus.DELIVERED and attempt.outcome_code != outcome:
                attempt.outcome_code = outcome  # settled before the stream ended: apply it now
                apply_outcome(session, session.get(CommunicationJob, attempt.job_id), outcome, now)
            session.commit()

    def _pipeline_failed(self, ended_by: str) -> None:
        """The voice pipeline itself failed (TTS): the attempt fails as VOICE_PIPELINE_FAILED -- retried with backoff,
        bounded by the policy's max attempts -- and the session ends. Taken by one conditional UPDATE."""
        self.ended = True
        now = self.clock()
        with self._session() as session:
            attempt, voice = self._rows(session)
            if attempt is None or voice is None:
                return
            taken = session.execute(update(CommunicationAttempt).where(
                CommunicationAttempt.id == attempt.id,
                CommunicationAttempt.status.in_([AttemptStatus.IN_PROGRESS, AttemptStatus.ANSWERED]),
            ).values(status=AttemptStatus.FAILED, error_code=PIPELINE_FAILED, completed_at=now, updated_at=now)
                .execution_options(synchronize_session=False))
            voice.status, voice.ended_at, voice.updated_at = VoiceSessionStatus.FAILED, voice.ended_at or now, now
            voice.outcome = self._summary(ended_by)
            if taken.rowcount == 1:
                job = session.get(CommunicationJob, attempt.job_id)
                if job.status == CommunicationJobStatus.IN_PROGRESS:
                    record_failed_attempt(session, job, PIPELINE_FAILED, now, self.setup.adapters, self.setup.policy)
            session.commit()


def voice_pipeline_from_env(config: Optional[VoiceProviderConfig] = None) -> Tuple[Optional[VoicePipeline], Optional[str]]:
    """The live Groq pipeline, or (None, reason). Fake speech is never substituted: without GROQ_API_KEY (or a TTS
    voice) voice streams are refused and the reason is reported."""
    config = config or VoiceProviderConfig.from_env()
    reason = config.unavailable_reason()
    if reason is not None:
        return None, reason
    http = GroqHTTP(config.api_key, timeout=config.timeout)
    return VoicePipeline(GroqSpeechToText(http, config.stt_model, config.language),
                         GroqTextToSpeech(http, config.tts_model, config.tts_voice),
                         GroqVoiceBrain(http, config.brain_model)), None
