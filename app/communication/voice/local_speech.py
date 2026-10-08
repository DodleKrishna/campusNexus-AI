"""Local / offline speech providers (AgentOS V2 Phase 6): whisper.cpp STT, and Kokoro or Piper TTS.

Kokoro (Phase 6.2A, ``CAMPUSNEXUS_LOCAL_TTS_PROVIDER=kokoro``) runs in-process through ``kokoro-onnx``: model and
voices files come only from ``CAMPUSNEXUS_KOKORO_MODEL_PATH`` / ``CAMPUSNEXUS_KOKORO_VOICES_PATH`` (absolute, existing),
the voice / speed (bounded 0.75-1.15) / language from env, and the engine is loaded once and reused. Its 24 kHz float
output is low-passed and normalized to linear16 mono 16 kHz like Piper's. Errors leave as codes only.

Both run an explicitly configured local executable through ``run_local_tool``:

* the executable and model paths come only from server environment variables (``CAMPUSNEXUS_WHISPER_CPP_PATH`` /
  ``CAMPUSNEXUS_WHISPER_MODEL_PATH``, ``CAMPUSNEXUS_PIPER_PATH`` / ``CAMPUSNEXUS_PIPER_MODEL_PATH``), must be absolute
  paths to existing files, and are never taken from a request, a transcript or a model;
* the argument vector is fixed (``whisper_argv`` / ``piper_argv``): no user text is ever an argument (Piper reads its
  text from stdin, whisper.cpp a temp WAV the code wrote), no ``shell=True``, a minimal environment, a finite timeout;
* input is bounded (audio bytes / text characters) and output is size-checked.

whisper.cpp needs a file: the utterance is written to a private temp file that is deleted in ``finally`` right after
the run. The transcript stays in memory (``Transcript`` hides it in ``repr``); nothing here logs or persists audio or
text. Neither binary nor model is downloaded or bundled: an unconfigured or missing one reports ``unavailable`` with a
safe code, and tests never need them.
"""
from __future__ import annotations

import array
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from app.communication.voice.audio import BYTES_PER_SECOND, SAMPLE_RATE, AudioFormatError, pcm_to_wav, wav_to_pcm
from app.communication.voice.speech import MAX_TRANSCRIPT_CHARS, SpeechProviderError, Transcript

WHISPER_PATH_ENV, WHISPER_MODEL_ENV = "CAMPUSNEXUS_WHISPER_CPP_PATH", "CAMPUSNEXUS_WHISPER_MODEL_PATH"
PIPER_PATH_ENV, PIPER_MODEL_ENV = "CAMPUSNEXUS_PIPER_PATH", "CAMPUSNEXUS_PIPER_MODEL_PATH"
LOCAL_TTS_PROVIDER_ENV = "CAMPUSNEXUS_LOCAL_TTS_PROVIDER"
LANGUAGE_ENV = "CAMPUSNEXUS_STT_LANGUAGE"
TIMEOUT_ENV = "CAMPUSNEXUS_LOCAL_SPEECH_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS = 30.0, 120.0
MAX_STT_SECONDS = 30  # one utterance
MAX_LOCAL_TTS_CHARS = 400
MAX_TTS_SECONDS = 30
MAX_STDOUT_BYTES = 64 * 1024  # whisper.cpp prints the text only (-nt -np)
DEFAULT_PIPER_RATE = 22050
KOKORO_MODEL_ENV, KOKORO_VOICES_ENV = "CAMPUSNEXUS_KOKORO_MODEL_PATH", "CAMPUSNEXUS_KOKORO_VOICES_PATH"
KOKORO_VOICE_ENV, KOKORO_SPEED_ENV = "CAMPUSNEXUS_KOKORO_VOICE", "CAMPUSNEXUS_KOKORO_SPEED"
KOKORO_LANGUAGE_ENV = "CAMPUSNEXUS_KOKORO_LANG"
DEFAULT_KOKORO_VOICE, DEFAULT_KOKORO_SPEED, DEFAULT_KOKORO_LANGUAGE = "af_heart", 0.88, "en-us"
MIN_KOKORO_SPEED, MAX_KOKORO_SPEED = 0.75, 1.15
KOKORO_LANGUAGES = frozenset({"en-us", "en-gb"})
KOKORO_SENTENCE_PAUSE, KOKORO_CLAUSE_PAUSE = 0.35, 0.15  # seconds at . ! ? and at , ; :
MAX_KOKORO_TTS_SECONDS = 45  # the slower default speed makes a 400-character reply longer than Piper's
KOKORO_FILTER_TAPS = 63
_KOKORO_VOICE = re.compile(r"[a-z]{2}_[a-z]{2,20}")
_LANGUAGE = re.compile(r"[a-z]{2,3}|auto")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WHISPER_MARKERS = re.compile(r"\[(?:BLANK_AUDIO|MUSIC|NOISE|SILENCE|INAUDIBLE)[^\]]*\]|\((?:silence|music)\)", re.I)


class LocalToolError(SpeechProviderError):
    """A local speech tool failure; ``code`` only (never the tool's stderr, which can echo input)."""


def trusted_file(env_name: str) -> Optional[Path]:
    """An absolute path to an existing regular file from server configuration, else None."""
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        return None
    return path


def _minimal_env() -> dict:
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")
    return {k: os.environ[k] for k in keep if k in os.environ}


def run_local_tool(argv: Sequence[str], *, stdin: Optional[bytes], timeout: float, max_output: int,
                   runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bytes:
    """Run one configured tool: argv list (never a shell string), finite timeout, bounded output. ``runner`` is the
    test seam (it receives exactly the keyword arguments used in production)."""
    kwargs = dict(input=stdin if stdin is not None else b"", capture_output=True, timeout=timeout, shell=False,
                  check=False, env=_minimal_env())
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = runner(list(argv), **kwargs)
    except subprocess.TimeoutExpired:
        raise LocalToolError("LOCAL_SPEECH_TIMEOUT", retryable=True) from None
    except OSError:
        raise LocalToolError("LOCAL_SPEECH_UNAVAILABLE") from None
    if completed.returncode != 0:
        raise LocalToolError("LOCAL_SPEECH_FAILED")
    output = completed.stdout or b""
    if len(output) > max_output:
        raise LocalToolError("LOCAL_SPEECH_OUTPUT_TOO_LARGE")
    return output


def _timeout() -> float:
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    if not 1.0 <= value <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"{TIMEOUT_ENV} must be 1-{MAX_TIMEOUT_SECONDS:g}")
    return value


# --- Speech to text: whisper.cpp ----------------------------------------------------------------------------------


def whisper_argv(executable: Path, model: Path, wav_path: str, language: str) -> List[str]:
    """The only argument vector ever passed to whisper.cpp: model, input file, language, text-only output."""
    return [str(executable), "-m", str(model), "-f", wav_path, "-l", language, "-nt", "-np"]


@dataclass(frozen=True)
class WhisperConfig:
    executable: Optional[Path]
    model: Optional[Path]
    language: str = "en"
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "WhisperConfig":
        language = (os.environ.get(LANGUAGE_ENV) or "en").strip().lower()
        if not _LANGUAGE.fullmatch(language):
            raise ValueError(f"{LANGUAGE_ENV} must be a language code")
        return cls(trusted_file(WHISPER_PATH_ENV), trusted_file(WHISPER_MODEL_ENV), language, _timeout())

    def unavailable_reason(self) -> Optional[str]:
        if self.executable is None:
            return "WHISPER_CPP_NOT_CONFIGURED"
        if self.model is None:
            return "WHISPER_MODEL_NOT_CONFIGURED"
        return None


class LocalWhisperSTTProvider:
    provider_name, model_name = "whisper_cpp", "whisper.cpp"

    def __init__(self, config: WhisperConfig, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.config, self._runner = config, runner

    def available(self) -> bool:
        return self.config.unavailable_reason() is None

    def transcribe(self, pcm: bytes) -> Transcript:
        reason = self.config.unavailable_reason()
        if reason is not None:
            raise LocalToolError(reason)
        if not pcm:
            return Transcript("", False)
        if len(pcm) > MAX_STT_SECONDS * BYTES_PER_SECOND:
            raise LocalToolError("STT_AUDIO_TOO_LONG")
        try:
            wav = pcm_to_wav(pcm)
        except AudioFormatError:
            raise LocalToolError("STT_BAD_AUDIO") from None
        fd, path = tempfile.mkstemp(prefix="cn-stt-", suffix=".wav")  # private (0600) temp file
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(wav)
            out = run_local_tool(whisper_argv(self.config.executable, self.config.model, path, self.config.language),
                                 stdin=None, timeout=self.config.timeout, max_output=MAX_STDOUT_BYTES,
                                 runner=self._runner)
        finally:
            try:
                os.remove(path)  # the utterance never outlives this call
            except OSError:
                pass
        text = " ".join(_WHISPER_MARKERS.sub(" ", out.decode("utf-8", errors="replace")).split())
        return Transcript(text[:MAX_TRANSCRIPT_CHARS], bool(text))


# --- Text to speech: Piper -----------------------------------------------------------------------------------------


def piper_argv(executable: Path, model: Path) -> List[str]:
    """The only argument vector ever passed to Piper. The text goes to stdin; raw 16-bit mono PCM comes back."""
    return [str(executable), "--model", str(model), "--output_raw"]


def _model_rate(model: Path) -> int:
    """Piper's voice sample rate from the model's ``.onnx.json`` (configured path + .json), else 22050."""
    try:
        config = json.loads(Path(f"{model}.json").read_text(encoding="utf-8"))
        rate = int(config["audio"]["sample_rate"])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_PIPER_RATE
    return rate if 8000 <= rate <= 48000 else DEFAULT_PIPER_RATE


def resample_linear16(pcm: bytes, source_rate: int, target_rate: int = SAMPLE_RATE) -> bytes:
    """Linear-interpolation resampling of signed 16-bit mono PCM (stdlib only)."""
    if source_rate == target_rate or not pcm:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    count = int(len(samples) * target_rate / source_rate)
    out = array.array("h", bytes(2 * count))
    step, last = source_rate / target_rate, len(samples) - 1
    for i in range(count):
        position = i * step
        left = int(position)
        right = min(left + 1, last)
        fraction = position - left
        out[i] = int(samples[left] + (samples[right] - samples[left]) * fraction)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


@dataclass(frozen=True)
class PiperConfig:
    executable: Optional[Path]
    model: Optional[Path]
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "PiperConfig":
        return cls(trusted_file(PIPER_PATH_ENV), trusted_file(PIPER_MODEL_ENV), _timeout())

    def unavailable_reason(self) -> Optional[str]:
        # Piper only; which local TTS provider is built is chosen by speech_router.local_tts_from_env.
        if self.executable is None:
            return "PIPER_NOT_CONFIGURED"
        if self.model is None:
            return "PIPER_MODEL_NOT_CONFIGURED"
        return None


class LocalTTSProvider:
    """Interface marker for local TTS: ``synthesize(text) -> linear16 mono 16 kHz PCM``."""

    provider_name: str = ""
    model_name: Optional[str] = None

    def available(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def synthesize(self, text: str) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError


class PiperTTSProvider(LocalTTSProvider):
    provider_name, model_name = "piper", "piper"

    def __init__(self, config: PiperConfig, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.config, self._runner = config, runner

    def available(self) -> bool:
        return self.config.unavailable_reason() is None

    def synthesize(self, text: str) -> bytes:
        reason = self.config.unavailable_reason()
        if reason is not None:
            raise LocalToolError(reason)
        text = " ".join(_CONTROL.sub(" ", text or "").split())
        if not text:
            raise LocalToolError("TTS_EMPTY_INPUT")
        if len(text) > MAX_LOCAL_TTS_CHARS:
            raise LocalToolError("TTS_INPUT_TOO_LONG")
        rate = _model_rate(self.config.model)
        raw = run_local_tool(piper_argv(self.config.executable, self.config.model), stdin=text.encode("utf-8") + b"\n",
                             timeout=self.config.timeout, max_output=MAX_TTS_SECONDS * rate * 2, runner=self._runner)
        if len(raw) < 2:
            raise LocalToolError("TTS_EMPTY_AUDIO")
        return resample_linear16(raw, rate)


# --- Text to speech: Kokoro (in-process ONNX, Phase 6.2A) ---------------------------------------------------------


def _kokoro_speed() -> float:
    raw = (os.environ.get(KOKORO_SPEED_ENV) or "").strip()
    value = float(raw) if raw else DEFAULT_KOKORO_SPEED
    if not MIN_KOKORO_SPEED <= value <= MAX_KOKORO_SPEED:  # also refuses nan
        raise ValueError(f"{KOKORO_SPEED_ENV} must be {MIN_KOKORO_SPEED:g}-{MAX_KOKORO_SPEED:g}")
    return value


@dataclass(frozen=True)
class KokoroConfig:
    model: Optional[Path]
    voices: Optional[Path]
    voice: str = DEFAULT_KOKORO_VOICE
    speed: float = DEFAULT_KOKORO_SPEED
    language: str = DEFAULT_KOKORO_LANGUAGE

    def __post_init__(self) -> None:
        if not _KOKORO_VOICE.fullmatch(self.voice):
            raise ValueError(f"{KOKORO_VOICE_ENV} must be a voice name such as af_heart")
        if not MIN_KOKORO_SPEED <= self.speed <= MAX_KOKORO_SPEED:
            raise ValueError(f"{KOKORO_SPEED_ENV} must be {MIN_KOKORO_SPEED:g}-{MAX_KOKORO_SPEED:g}")
        if self.language not in KOKORO_LANGUAGES:
            raise ValueError(f"{KOKORO_LANGUAGE_ENV} must be one of {', '.join(sorted(KOKORO_LANGUAGES))}")

    @classmethod
    def from_env(cls) -> "KokoroConfig":
        voice = (os.environ.get(KOKORO_VOICE_ENV) or DEFAULT_KOKORO_VOICE).strip().lower()
        language = (os.environ.get(KOKORO_LANGUAGE_ENV) or DEFAULT_KOKORO_LANGUAGE).strip().lower()
        return cls(trusted_file(KOKORO_MODEL_ENV), trusted_file(KOKORO_VOICES_ENV), voice, _kokoro_speed(), language)

    def unavailable_reason(self) -> Optional[str]:
        if self.model is None:
            return "KOKORO_MODEL_NOT_CONFIGURED"
        if self.voices is None:
            return "KOKORO_VOICES_NOT_CONFIGURED"
        if not kokoro_installed():
            return "KOKORO_NOT_INSTALLED"
        return None


def kokoro_installed() -> bool:
    """Whether ``kokoro-onnx`` can be imported (checked without loading the model, so health stays cheap)."""
    return importlib.util.find_spec("kokoro_onnx") is not None


def load_kokoro(model: Path, voices: Path) -> Any:
    """The real engine: one ONNX session on the local CPU (no download, no network)."""
    from kokoro_onnx import Kokoro

    package_log = logging.getLogger("kokoro_onnx")
    if package_log.getEffectiveLevel() < logging.WARNING:  # its DEBUG lines echo the phonemes of the reply text
        package_log.setLevel(logging.WARNING)
    return Kokoro(str(model), str(voices))


def _anti_alias(samples: Any, rate: int) -> Any:
    """Low-pass float samples below the 16 kHz Nyquist frequency before ``resample_linear16`` decimates them."""
    import numpy as np

    if rate <= SAMPLE_RATE:
        return samples
    cutoff = 0.92 * (SAMPLE_RATE / 2) / rate  # cycles per source sample
    n = np.arange(KOKORO_FILTER_TAPS) - (KOKORO_FILTER_TAPS - 1) / 2
    taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(KOKORO_FILTER_TAPS)
    return np.convolve(samples, taps / taps.sum(), mode="same")


def kokoro_samples_to_pcm(samples: Any, rate: int) -> bytes:
    """Kokoro's float samples -> the one internal format (linear16 mono 16 kHz), checked by the WAV round trip."""
    import numpy as np

    if not 8000 <= int(rate) <= 48000:
        raise LocalToolError("TTS_BAD_AUDIO")
    data = np.nan_to_num(np.asarray(samples, dtype=np.float32).ravel())
    if data.size == 0:
        raise LocalToolError("TTS_EMPTY_AUDIO")
    if data.size > MAX_KOKORO_TTS_SECONDS * int(rate):
        raise LocalToolError("TTS_AUDIO_TOO_LONG")
    pcm = (np.clip(_anti_alias(data, int(rate)), -1.0, 1.0) * 32767).astype("<i2").tobytes()
    try:
        return wav_to_pcm(pcm_to_wav(resample_linear16(pcm, int(rate))),
                          max_bytes=MAX_KOKORO_TTS_SECONDS * BYTES_PER_SECOND)
    except AudioFormatError as exc:
        raise LocalToolError(str(exc)) from None


class KokoroTTSProvider(LocalTTSProvider):
    """Kokoro in this process. The engine is loaded on first use and reused (the router lives for the app)."""

    provider_name, model_name = "kokoro", "kokoro-onnx"

    def __init__(self, config: KokoroConfig, *, loader: Callable[[Path, Path], Any] = load_kokoro) -> None:
        self.config, self._loader = config, loader
        self._engine: Any = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.config.unavailable_reason() is None

    def _loaded(self) -> Any:
        with self._lock:
            if self._engine is None:
                try:
                    engine = self._loader(self.config.model, self.config.voices)
                except Exception:  # onnxruntime/espeak errors can carry paths; only the code leaves
                    raise LocalToolError("KOKORO_LOAD_FAILED") from None
                if self.config.voice not in engine.voices:
                    raise LocalToolError("KOKORO_VOICE_NOT_FOUND")
                self._engine = engine
            return self._engine

    def synthesize(self, text: str) -> bytes:
        reason = self.config.unavailable_reason()
        if reason is not None:
            raise LocalToolError(reason)
        text = " ".join(_CONTROL.sub(" ", text or "").split())
        if not text:
            raise LocalToolError("TTS_EMPTY_INPUT")
        if len(text) > MAX_LOCAL_TTS_CHARS:
            raise LocalToolError("TTS_INPUT_TOO_LONG")
        engine = self._loaded()
        try:
            samples, rate = engine.create(text, voice=self.config.voice, speed=self.config.speed,
                                          lang=self.config.language, sentence_pause=KOKORO_SENTENCE_PAUSE,
                                          clause_pause=KOKORO_CLAUSE_PAUSE)
        except Exception:  # its messages can quote the text or its phonemes
            raise LocalToolError("LOCAL_SPEECH_FAILED") from None
        return kokoro_samples_to_pcm(samples, rate)
