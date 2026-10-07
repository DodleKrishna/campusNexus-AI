"""The one internal telephone audio format (AgentOS V2 Phase 5.1) and strict conversions to and from it.

Everything inside the voice pipeline is PCM signed 16-bit little-endian, mono, 16000 Hz ("linear16"). Bytes are
never guessed: Exotel's ``start.media_format`` is validated before any media is accepted, and a WAV returned by a
TTS provider is parsed with the stdlib ``wave`` module and rejected unless it is exactly this format. Conversions
happen in memory (``io.BytesIO``); nothing here writes audio to disk or keeps it.
"""
from __future__ import annotations

import io
import wave
from typing import Any, Iterator, Mapping

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # bytes: signed 16-bit
CHANNELS = 1
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS  # 32000
FRAME_MS = 20
FRAME_BYTES = BYTES_PER_SECOND * FRAME_MS // 1000  # 640: one 20 ms VAD frame
PLAYBACK_CHUNK_BYTES = 3200  # 100 ms per outbound media message (a multiple of 320, as Exotel expects)

# Exotel names the payload encoding in a few ways; all mean base64 of raw signed-linear 16-bit LE PCM.
ACCEPTED_ENCODINGS = frozenset({"base64", "raw/slin", "slin", "audio/x-l16", "linear16", "pcm_s16le", "audio/l16"})
ACCEPTED_BIT_RATES = frozenset({"256", "256kbps", "256000"})


class AudioFormatError(ValueError):
    """Audio (or a declared format) that is not linear16 mono 16 kHz. ``str(exc)`` is a safe code."""


def validate_media_format(media_format: Any) -> None:
    """Exotel ``start.media_format``: encoding must be raw linear16 and sample_rate 16000 (bit_rate, if sent, 256 kbps)."""
    if not isinstance(media_format, Mapping):
        raise AudioFormatError("MEDIA_FORMAT_MISSING")
    encoding = str(media_format.get("encoding") or "").strip().lower()
    if encoding not in ACCEPTED_ENCODINGS:
        raise AudioFormatError("UNSUPPORTED_ENCODING")
    try:
        rate = int(str(media_format.get("sample_rate") or "").strip())
    except ValueError:
        raise AudioFormatError("UNSUPPORTED_SAMPLE_RATE") from None
    if rate != SAMPLE_RATE:
        raise AudioFormatError("UNSUPPORTED_SAMPLE_RATE")
    bit_rate = media_format.get("bit_rate")
    if bit_rate not in (None, "") and str(bit_rate).strip().lower() not in ACCEPTED_BIT_RATES:
        raise AudioFormatError("UNSUPPORTED_BIT_RATE")


def pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap linear16 mono 16 kHz PCM in an in-memory WAV container (for the STT upload)."""
    if len(pcm) % SAMPLE_WIDTH:
        raise AudioFormatError("PCM_ODD_LENGTH")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


def wav_to_pcm(data: bytes, *, max_bytes: int) -> bytes:
    """The PCM frames of a WAV that is exactly mono / 16-bit / 16 kHz (headers removed). Anything else is rejected."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getcomptype() != "NONE":
                raise AudioFormatError("TTS_COMPRESSED_AUDIO")
            if wav.getnchannels() != CHANNELS:
                raise AudioFormatError("TTS_NOT_MONO")
            if wav.getsampwidth() != SAMPLE_WIDTH:
                raise AudioFormatError("TTS_NOT_16_BIT")
            if wav.getframerate() != SAMPLE_RATE:
                raise AudioFormatError("TTS_WRONG_SAMPLE_RATE")
            # Read at most the bound (a streamed WAV header may carry a placeholder frame count).
            pcm = wav.readframes(max_bytes // SAMPLE_WIDTH + 1)
    except (wave.Error, EOFError):
        raise AudioFormatError("TTS_NOT_WAV") from None
    if len(pcm) > max_bytes:
        raise AudioFormatError("TTS_AUDIO_TOO_LONG")
    if not pcm:
        raise AudioFormatError("TTS_EMPTY_AUDIO")
    return pcm


def upload_wav_to_pcm(data: bytes, *, max_seconds: int) -> bytes:
    """Phase 6 in-app voice: an uploaded WAV must be exactly linear16 mono 16 kHz and at most ``max_seconds`` long.
    Codes are ``AUDIO_*`` (e.g. AUDIO_NOT_WAV, AUDIO_WRONG_SAMPLE_RATE, AUDIO_TOO_LONG)."""
    try:
        return wav_to_pcm(data, max_bytes=max_seconds * BYTES_PER_SECOND)
    except AudioFormatError as exc:
        code = str(exc).removeprefix("TTS_")
        raise AudioFormatError(code if code.startswith("AUDIO_") else f"AUDIO_{code}") from None


def playback_chunks(pcm: bytes) -> Iterator[bytes]:
    for start in range(0, len(pcm), PLAYBACK_CHUNK_BYTES):
        yield pcm[start:start + PLAYBACK_CHUNK_BYTES]


def duration_seconds(pcm_bytes: int) -> float:
    return pcm_bytes / BYTES_PER_SECOND
