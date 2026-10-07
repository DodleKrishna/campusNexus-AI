"""In-app Nexus voice (AgentOS V2 Phase 6): one bounded WAV utterance -> STT -> Nexus -> reply -> TTS.

audio (linear16 mono 16 kHz WAV, at most ``MAX_VOICE_SECONDS``) -> the configured STT provider (``SpeechRouter``:
cloud / local whisper.cpp / auto) -> the existing ``PersonalAssistant`` (one Nexus mission, the same Agent Kernel,
tools and audit) -> the user-visible reply -> the configured TTS provider -> WAV audio.

Identity comes only from the caller's verified token (``MissionActor``). The transcript lives in memory only: the
mission stores a fixed placeholder goal (``PersonalAssistant.handle_message(store_message=False)``) and nothing here
logs or persists the audio, the transcript or the reply audio. A failed TTS still returns the text reply, with
``audio_error_code``. No reasoning is ever returned.
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agentos.nexus import AssistantReply, PersonalAssistant
from app.agentos.runtime import AgentOSError, MissionActor
from app.communication.voice.audio import AudioFormatError, pcm_to_wav, upload_wav_to_pcm
from app.communication.voice.speech import SpeechProviderError
from app.llm.router import ai_context

MAX_VOICE_SECONDS = 30
MAX_UPLOAD_BYTES = MAX_VOICE_SECONDS * 32000 + 4096  # 30 s of linear16 mono 16 kHz plus a WAV header
MAX_SPOKEN_CHARS = 200  # the strictest TTS input limit (Groq Orpheus); the full text is still returned
WAV_CONTENT_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"})


class SpeechInfo(BaseModel):
    stt_provider: Optional[str] = None
    tts_provider: Optional[str] = None


class VoiceAssistantReply(AssistantReply):
    speech: SpeechInfo
    audio_wav_base64: Optional[str] = None  # the spoken reply (16 kHz mono WAV), when TTS succeeded
    audio_error_code: Optional[str] = None


def spoken_text(message: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """The reply clipped for speech at a sentence or word boundary."""
    text = " ".join((message or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: end + 1] if end >= limit // 2 else cut[: cut.rfind(" ")].rstrip(",;:") + "..."


class VoiceAssistant:
    def __init__(self, assistant: PersonalAssistant, speech: Any) -> None:
        self.assistant, self.speech = assistant, speech

    def handle(self, session: Session, actor: MissionActor, wav: bytes, *, recorder: Any = None,
               reply_audio: bool = True) -> VoiceAssistantReply:
        """Raises ``AgentOSError`` (bad audio 400/413, speech unavailable 503, not understood 422) or the assistant's
        own ``AssistantUnavailable``."""
        if len(wav) > MAX_UPLOAD_BYTES:
            raise AgentOSError("AUDIO_TOO_LARGE", "The recording is too long.", 413)
        try:
            pcm = upload_wav_to_pcm(wav, max_seconds=MAX_VOICE_SECONDS)
        except AudioFormatError as exc:
            raise AgentOSError(str(exc), "Send a 16 kHz mono 16-bit WAV recording.", 400) from None
        speech = SpeechInfo()
        with ai_context(actor.organization_id, recorder=recorder):  # speech telemetry of this request (no content)
            try:
                transcript, speech.stt_provider = self.speech.transcribe(pcm)
            except SpeechProviderError as exc:
                raise AgentOSError(exc.code, "Speech recognition is unavailable right now. Nothing was executed.",
                                   503) from None
            del pcm
            if not transcript.usable:
                raise AgentOSError("SPEECH_NOT_UNDERSTOOD", "The recording could not be understood. Please try again.",
                                   422)
            reply = self.assistant.handle_message(session, actor, transcript.text, recorder=recorder,
                                                  store_message=False)
            del transcript
            result = VoiceAssistantReply(**reply.model_dump(), speech=speech)
            if reply_audio and reply.assistant_message:
                try:
                    audio, speech.tts_provider = self.speech.synthesize(spoken_text(reply.assistant_message))
                    result.audio_wav_base64 = base64.b64encode(pcm_to_wav(audio)).decode("ascii")
                except (SpeechProviderError, AudioFormatError) as exc:
                    result.audio_error_code = getattr(exc, "code", None) or str(exc)
            result.speech = speech
        return result
