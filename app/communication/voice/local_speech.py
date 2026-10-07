"""Local / offline speech providers (AgentOS V2 Phase 6): whisper.cpp STT and Piper TTS.

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
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from app.communication.voice.audio import BYTES_PER_SECOND, SAMPLE_RATE, AudioFormatError, pcm_to_wav
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
        provider = (os.environ.get(LOCAL_TTS_PROVIDER_ENV) or "piper").strip().lower()
        if provider != "piper":
            return "LOCAL_TTS_PROVIDER_UNSUPPORTED"
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
