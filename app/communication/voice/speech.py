"""Speech providers for live calls (AgentOS V2 Phase 5.1): Groq Whisper STT and Groq Orpheus TTS.

Both speak Groq's OpenAI-compatible audio API over ``httpx`` (the same approach as ``app.llm.providers.groq``; no
vendor SDK). Voice is real-time, so the provider policy is deliberately small: a finite timeout, at most
``max_retries`` (default 1) immediate retry of a timeout / network error / 5xx, never a wait on a rate limit.
Every failure is a ``SpeechProviderError`` carrying only a code -- never the audio, the text, the response body or
the key.

* STT: one utterance of linear16 mono 16 kHz PCM is wrapped in an in-memory WAV (``audio.pcm_to_wav``) and
  transcribed. The text is returned in a ``Transcript`` whose ``repr`` hides it; an empty or low-confidence result is
  marked unusable so it never advances the conversation.
* TTS: a short reply (``MAX_TTS_CHARS``) is synthesized as WAV at 16 kHz; the WAV is parsed and validated
  (``audio.wav_to_pcm``) and only its PCM frames are returned. Incompatible audio fails explicitly.

Configuration comes only from the environment (``VoiceProviderConfig.from_env``); ``GROQ_API_KEY`` is reused.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from app.communication.voice.audio import BYTES_PER_SECOND, AudioFormatError, SAMPLE_RATE, pcm_to_wav, wav_to_pcm

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_STT_MODEL = "whisper-large-v3-turbo"
DEFAULT_TTS_MODEL = "canopylabs/orpheus-v1-english"
DEFAULT_BRAIN_MODEL = "openai/gpt-oss-20b"
DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_TTS_CHARS = 200  # Orpheus input limit; conversation replies are capped lower (180)
MAX_TTS_SECONDS = 20
MAX_TRANSCRIPT_CHARS = 500
# Whisper verbose_json confidence signals: a segment is "no speech" above this probability, and an average
# log-probability below the floor means the words are guesses.
NO_SPEECH_THRESHOLD = 0.6
AVG_LOGPROB_FLOOR = -1.0

ENV = {
    "stt_provider": "CAMPUSNEXUS_STT_PROVIDER", "tts_provider": "CAMPUSNEXUS_TTS_PROVIDER",
    "stt_model": "CAMPUSNEXUS_STT_MODEL", "tts_model": "CAMPUSNEXUS_TTS_MODEL", "tts_voice": "CAMPUSNEXUS_TTS_VOICE",
    "brain_model": "CAMPUSNEXUS_VOICE_BRAIN_MODEL", "language": "CAMPUSNEXUS_STT_LANGUAGE",
    "timeout": "CAMPUSNEXUS_VOICE_PROVIDER_TIMEOUT_SECONDS",
}


class SpeechProviderError(Exception):
    """A speech/voice-brain provider failure. ``code`` is safe to persist; nothing else is carried."""

    def __init__(self, code: str, retryable: bool = False) -> None:
        super().__init__(code)
        self.code, self.retryable = code, retryable


@dataclass(frozen=True)
class VoiceProviderConfig:
    stt_provider: str = "groq"
    tts_provider: str = "groq"
    stt_model: str = DEFAULT_STT_MODEL
    tts_model: str = DEFAULT_TTS_MODEL
    tts_voice: str = ""
    brain_model: str = DEFAULT_BRAIN_MODEL
    language: str = "en"
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "VoiceProviderConfig":
        env = os.environ.get
        timeout = float(env(ENV["timeout"]) or DEFAULT_TIMEOUT_SECONDS)
        if not 1.0 <= timeout <= 30.0:
            raise ValueError(f"{ENV['timeout']} must be 1-30 seconds")
        return cls(stt_provider=(env(ENV["stt_provider"]) or "groq").strip().lower(),
                   tts_provider=(env(ENV["tts_provider"]) or "groq").strip().lower(),
                   stt_model=env(ENV["stt_model"]) or DEFAULT_STT_MODEL,
                   tts_model=env(ENV["tts_model"]) or DEFAULT_TTS_MODEL,
                   tts_voice=(env(ENV["tts_voice"]) or "").strip(),
                   brain_model=env(ENV["brain_model"]) or DEFAULT_BRAIN_MODEL,
                   language=(env(ENV["language"]) or "en").strip(), timeout=timeout,
                   api_key=env("GROQ_API_KEY") or "")

    def unavailable_reason(self) -> Optional[str]:
        """None when the live pipeline can be built; else a safe code. Fake speech is never a fallback."""
        if self.stt_provider != "groq" or self.tts_provider != "groq":
            return "VOICE_PROVIDER_UNSUPPORTED"
        if not self.api_key:
            return "GROQ_API_KEY_MISSING"
        if not self.tts_voice:
            return "TTS_VOICE_NOT_CONFIGURED"
        return None


class GroqHTTP:
    """Minimal Groq HTTP plumbing for voice. ``client`` is a test seam (an ``httpx.Client``-like ``post``)."""

    def __init__(self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, base_url: str = DEFAULT_BASE_URL,
                 client: Any = None, max_retries: int = 1) -> None:
        if not 0 <= max_retries <= 2:
            raise ValueError("max_retries must be 0-2")
        self.max_retries = max_retries
        if client is not None:
            self._client, self._httpx = client, None
            return
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)

    def post(self, path: str, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            try:
                response = self._client.post(path, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- transport errors are classified, never echoed
                code = "PROVIDER_TIMEOUT" if "Timeout" in type(exc).__name__ else "PROVIDER_UNAVAILABLE"
                if attempt < self.max_retries:
                    attempt += 1
                    continue
                raise SpeechProviderError(code, retryable=True) from None
            status = response.status_code
            if status >= 500 and attempt < self.max_retries:
                attempt += 1
                continue
            if status in (401, 403):
                raise SpeechProviderError("PROVIDER_AUTH_FAILED")
            if status == 429:
                raise SpeechProviderError("PROVIDER_RATE_LIMITED", retryable=True)
            if status >= 500:
                raise SpeechProviderError("PROVIDER_UNAVAILABLE", retryable=True)
            if status >= 400:
                raise SpeechProviderError("PROVIDER_REJECTED")
            return response


# --- Speech to text ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Transcript:
    """Transient: lives in memory for one call only. ``repr`` never shows the words."""

    text: str = field(repr=False)
    usable: bool

    def __repr__(self) -> str:
        return f"Transcript(chars={len(self.text)}, usable={self.usable})"

    __str__ = __repr__


class SpeechToTextProvider(Protocol):
    def transcribe(self, pcm: bytes) -> Transcript: ...


class GroqSpeechToText:
    def __init__(self, http: GroqHTTP, model: str = DEFAULT_STT_MODEL, language: str = "en") -> None:
        self.http, self.model, self.language = http, model, language

    def transcribe(self, pcm: bytes) -> Transcript:
        if not pcm:
            return Transcript("", False)
        try:
            wav = pcm_to_wav(pcm)
        except AudioFormatError:
            raise SpeechProviderError("STT_BAD_AUDIO") from None
        data = {"model": self.model, "response_format": "verbose_json", "temperature": "0"}
        if self.language:
            data["language"] = self.language
        response = self.http.post("/audio/transcriptions", data=data,
                                  files={"file": ("utterance.wav", wav, "audio/wav")})
        try:
            body = response.json()
        except ValueError:
            raise SpeechProviderError("STT_BAD_RESPONSE") from None
        text = " ".join(str(body.get("text") or "").split())[:MAX_TRANSCRIPT_CHARS]
        return Transcript(text, bool(text) and _confident(body.get("segments")))


def _confident(segments: Any) -> bool:
    if not isinstance(segments, list) or not segments:
        return True  # no per-segment signal: trust a non-empty text
    try:
        no_speech = [float(s.get("no_speech_prob", 0.0)) for s in segments]
        logprob = [float(s.get("avg_logprob", 0.0)) for s in segments]
    except (TypeError, ValueError, AttributeError):
        return False
    if all(p > NO_SPEECH_THRESHOLD for p in no_speech):
        return False
    return sum(logprob) / len(logprob) >= AVG_LOGPROB_FLOOR


# --- Text to speech ------------------------------------------------------------------------------------------------


class TextToSpeechProvider(Protocol):
    def synthesize(self, text: str) -> bytes:
        """Linear16 mono 16 kHz PCM (no container)."""


class GroqTextToSpeech:
    def __init__(self, http: GroqHTTP, model: str = DEFAULT_TTS_MODEL, voice: str = "") -> None:
        if not voice:
            raise ValueError("a TTS voice must be configured")
        self.http, self.model, self.voice = http, model, voice

    def synthesize(self, text: str) -> bytes:
        text = " ".join((text or "").split())
        if not text:
            raise SpeechProviderError("TTS_EMPTY_INPUT")
        if len(text) > MAX_TTS_CHARS:
            raise SpeechProviderError("TTS_INPUT_TOO_LONG")
        body: Dict[str, Any] = {"model": self.model, "input": text, "voice": self.voice, "response_format": "wav",
                                "sample_rate": SAMPLE_RATE}
        response = self.http.post("/audio/speech", json=body)
        try:
            return wav_to_pcm(response.content, max_bytes=MAX_TTS_SECONDS * BYTES_PER_SECOND)
        except AudioFormatError as exc:
            raise SpeechProviderError(str(exc)) from None
