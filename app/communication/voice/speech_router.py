"""Speech provider routing and speech telemetry (AgentOS V2 Phase 6).

``CAMPUSNEXUS_SPEECH_MODE`` (STT) and ``CAMPUSNEXUS_TTS_MODE`` (TTS; defaults to the speech mode) choose between the
cloud providers (Groq Whisper / Groq Orpheus, Phase 5.1) and the local ones (whisper.cpp / Kokoro or Piper):

* ``cloud`` -- cloud only; refused in the offline mode, never a local substitute.
* ``local`` -- local only; zero cloud speech calls.
* ``auto``  -- local while the deployment is offline (``ConnectivityService``) or the cloud provider is not configured;
  the cloud provider otherwise, moving to local only after a *transport* failure (``PROVIDER_UNAVAILABLE`` /
  ``PROVIDER_TIMEOUT``). An unconfigured provider is reported unavailable: fake speech is never used.

``MeteredSTT`` / ``MeteredTTS`` / ``MeteredVoiceBrain`` record one ``UsageRecord`` per call into the current AI
context (provider, model, latency, success, error code, audio duration in ms). Never the audio, the transcript, the
reply text or a prompt. Cost stays unavailable (``None``).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.agentos.connectivity import ConnectivityService, ConnectivityState, cloud_allowed
from app.communication.voice.audio import BYTES_PER_SECOND
from app.communication.voice.local_speech import (
    LOCAL_TTS_PROVIDER_ENV, KokoroConfig, KokoroTTSProvider, LocalWhisperSTTProvider, PiperConfig, PiperTTSProvider,
    WhisperConfig,
)
from app.communication.voice.speech import (
    GroqHTTP, GroqSpeechToText, GroqTextToSpeech, SpeechProviderError, Transcript, VoiceProviderConfig,
)
from app.llm.router import UsageRecord, current_ai_context
from app.schemas.enums import IntelligenceLevel

SPEECH_MODE_ENV, TTS_MODE_ENV = "CAMPUSNEXUS_SPEECH_MODE", "CAMPUSNEXUS_TTS_MODE"
CLOUD, LOCAL, AUTO = "cloud", "local", "auto"
MODES = (CLOUD, LOCAL, AUTO)
LOCAL_TTS_PROVIDERS = ("kokoro", "piper")
TRANSPORT_CODES = frozenset({"PROVIDER_UNAVAILABLE", "PROVIDER_TIMEOUT"})


# --- Telemetry -----------------------------------------------------------------------------------------------------


def record_speech(operation: str, provider: str, model: Optional[str], started: float, success: bool,
                  error_kind: Optional[str], audio_bytes: Optional[int] = None) -> None:
    ai = current_ai_context()
    if ai is None:
        return
    ai.records.append(UsageRecord(
        operation=operation, level=IntelligenceLevel.LIGHT, model=model, provider=provider, mission_id=ai.mission_id,
        agent_key=ai.agent_key, run_id=ai.run_id, input_tokens=None, output_tokens=None,
        latency_ms=int((time.perf_counter() - started) * 1000), success=success, error_kind=error_kind,
        estimated_cost_usd=None, created_at=datetime.now(timezone.utc),
        audio_ms=int(audio_bytes * 1000 / BYTES_PER_SECOND) if audio_bytes else None,
    ))


class MeteredSTT:
    def __init__(self, inner: Any, provider: str, model: Optional[str]) -> None:
        self.inner, self.provider_name, self.model_name = inner, provider, model

    def available(self) -> bool:
        check = getattr(self.inner, "available", None)
        return bool(check()) if callable(check) else True

    def transcribe(self, pcm: bytes) -> Transcript:
        started, error = time.perf_counter(), None
        try:
            return self.inner.transcribe(pcm)
        except SpeechProviderError as exc:
            error = exc.code
            raise
        finally:
            record_speech("voice_stt", self.provider_name, self.model_name, started, error is None, error, len(pcm or b""))


class MeteredTTS:
    def __init__(self, inner: Any, provider: str, model: Optional[str]) -> None:
        self.inner, self.provider_name, self.model_name = inner, provider, model

    def available(self) -> bool:
        check = getattr(self.inner, "available", None)
        return bool(check()) if callable(check) else True

    def synthesize(self, text: str) -> bytes:
        started, error, pcm = time.perf_counter(), None, b""
        try:
            pcm = self.inner.synthesize(text)
            return pcm
        except SpeechProviderError as exc:
            error = exc.code
            raise
        finally:
            record_speech("voice_tts", self.provider_name, self.model_name, started, error is None, error, len(pcm))


class MeteredVoiceBrain:
    """The Phase 5.1 restricted call brain, metered (operation ``voice_brain``)."""

    def __init__(self, inner: Any, provider: str, model: Optional[str]) -> None:
        self.inner, self.provider_name, self.model_name = inner, provider, model

    def decide(self, context: Any) -> Any:
        started, error = time.perf_counter(), None
        try:
            return self.inner.decide(context)
        except SpeechProviderError as exc:
            error = exc.code
            raise
        finally:
            record_speech("voice_brain", self.provider_name, self.model_name, started, error is None, error)


# --- Routing -------------------------------------------------------------------------------------------------------


def _mode(name: str, default: str) -> str:
    value = (os.environ.get(name) or default).strip().lower()
    if value not in MODES:
        raise ValueError(f"{name} must be one of {', '.join(MODES)}")
    return value


class SpeechRouter:
    def __init__(self, *, stt_mode: str, tts_mode: str, connectivity: ConnectivityService,
                 cloud_stt: Any = None, local_stt: Any = None, cloud_tts: Any = None, local_tts: Any = None) -> None:
        if stt_mode not in MODES or tts_mode not in MODES:
            raise ValueError("invalid speech mode")
        self.stt_mode, self.tts_mode, self.connectivity = stt_mode, tts_mode, connectivity
        self.cloud_stt, self.local_stt = (cloud_stt if stt_mode != LOCAL else None), (local_stt if stt_mode != CLOUD else None)
        self.cloud_tts, self.local_tts = (cloud_tts if tts_mode != LOCAL else None), (local_tts if tts_mode != CLOUD else None)

    @staticmethod
    def _ready(provider: Any) -> bool:
        return provider is not None and provider.available()

    def _plan(self, mode: str, cloud: Any, local: Any, kind: str) -> Tuple[Any, Any]:
        """(provider to use, local provider to move to after a cloud transport failure or None)."""
        if mode == LOCAL:
            if not self._ready(local):
                raise SpeechProviderError(f"LOCAL_{kind}_UNAVAILABLE")
            return local, None
        if not cloud_allowed():
            if mode == CLOUD:
                raise SpeechProviderError("CLOUD_DISABLED_OFFLINE")
            cloud = None
        if mode == CLOUD:
            if cloud is None:
                raise SpeechProviderError(f"CLOUD_{kind}_NOT_CONFIGURED")
            return cloud, None
        offline = cloud is None or self.connectivity.state() == ConnectivityState.LOCAL_ONLY
        if offline:
            if not self._ready(local):
                raise SpeechProviderError(f"LOCAL_{kind}_UNAVAILABLE")
            return local, None
        return cloud, (local if self._ready(local) else None)

    def _run(self, mode: str, cloud: Any, local: Any, kind: str, call: str, arg: Any) -> Tuple[Any, str]:
        provider, fallback = self._plan(mode, cloud, local, kind)
        try:
            return getattr(provider, call)(arg), provider.provider_name
        except SpeechProviderError as exc:
            if fallback is None or exc.code not in TRANSPORT_CODES:
                raise
            self.connectivity.invalidate()
        return getattr(fallback, call)(arg), fallback.provider_name

    def transcribe(self, pcm: bytes) -> Tuple[Transcript, str]:
        return self._run(self.stt_mode, self.cloud_stt, self.local_stt, "STT", "transcribe", pcm)

    def synthesize(self, text: str) -> Tuple[bytes, str]:
        return self._run(self.tts_mode, self.cloud_tts, self.local_tts, "TTS", "synthesize", text)

    def status(self) -> Dict[str, Any]:
        """Safe for health: provider names and readiness only (no paths, URLs or keys)."""
        def pick(mode: str, cloud: Any, local: Any) -> Optional[str]:
            if mode == LOCAL or (mode == AUTO and (cloud is None or not cloud_allowed()
                                                    or self.connectivity.state() == ConnectivityState.LOCAL_ONLY)):
                return local.provider_name if self._ready(local) else None
            return cloud.provider_name if cloud is not None and cloud_allowed() else None

        return {"stt_mode": self.stt_mode, "tts_mode": self.tts_mode,
                "stt_provider": pick(self.stt_mode, self.cloud_stt, self.local_stt),
                "tts_provider": pick(self.tts_mode, self.cloud_tts, self.local_tts),
                "local_ready": self._ready(self.local_stt) and self._ready(self.local_tts)}


def local_tts_from_env() -> MeteredTTS:
    """``CAMPUSNEXUS_LOCAL_TTS_PROVIDER``: kokoro or piper (unset = piper). Exactly one is built, so a configured but
    unavailable provider reports unavailable instead of quietly using the other one."""
    provider = (os.environ.get(LOCAL_TTS_PROVIDER_ENV) or "piper").strip().lower()
    if provider not in LOCAL_TTS_PROVIDERS:
        raise ValueError(f"LOCAL_TTS_PROVIDER_UNSUPPORTED: {LOCAL_TTS_PROVIDER_ENV} must be one of "
                         f"{', '.join(LOCAL_TTS_PROVIDERS)}")
    if provider == "kokoro":
        return MeteredTTS(KokoroTTSProvider(KokoroConfig.from_env()), "kokoro", KokoroTTSProvider.model_name)
    return MeteredTTS(PiperTTSProvider(PiperConfig.from_env()), "piper", "piper")


def speech_router_from_env(connectivity: ConnectivityService) -> SpeechRouter:
    """Raises ``ValueError`` on an invalid mode/timeout. Unconfigured providers stay None / report unavailable."""
    stt_mode = _mode(SPEECH_MODE_ENV, CLOUD)
    tts_mode = _mode(TTS_MODE_ENV, stt_mode)
    cloud_stt = cloud_tts = None
    if cloud_allowed() and (stt_mode, tts_mode) != (LOCAL, LOCAL):  # local-only builds no cloud client at all
        config = VoiceProviderConfig.from_env()
        if config.api_key:
            http = GroqHTTP(config.api_key, timeout=config.timeout)
            cloud_stt = MeteredSTT(GroqSpeechToText(http, config.stt_model, config.language), "groq", config.stt_model)
            if config.tts_voice:
                cloud_tts = MeteredTTS(GroqTextToSpeech(http, config.tts_model, config.tts_voice), "groq",
                                       config.tts_model)
    local_stt = MeteredSTT(LocalWhisperSTTProvider(WhisperConfig.from_env()), "whisper_cpp", "whisper.cpp")
    local_tts = local_tts_from_env()
    return SpeechRouter(stt_mode=stt_mode, tts_mode=tts_mode, connectivity=connectivity, cloud_stt=cloud_stt,
                        local_stt=local_stt, cloud_tts=cloud_tts, local_tts=local_tts)
