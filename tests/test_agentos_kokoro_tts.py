"""AgentOS V2 Phase 6.2A: Kokoro ONNX as the preferred local TTS provider (Piper stays an explicit option).

A fake engine stands in for ``kokoro_onnx.Kokoro`` (same ``voices`` / ``create`` surface), so no model is needed.
The last test runs the real model only when ``CAMPUSNEXUS_KOKORO_MODEL_PATH`` / ``_VOICES_PATH`` are configured and
``kokoro-onnx`` is installed. Sockets are refused in the synthesis tests, so any network use would surface.
"""
from __future__ import annotations

import logging
import math
import os
import socket
import tempfile
from types import SimpleNamespace

import numpy as np
import pytest

from app.communication.voice import local_speech
from app.communication.voice.audio import BYTES_PER_SECOND, SAMPLE_RATE, pcm_to_wav, wav_to_pcm
from app.communication.voice.local_speech import (
    KokoroConfig, KokoroTTSProvider, LocalToolError, PiperTTSProvider, kokoro_samples_to_pcm,
)
from app.communication.voice.speech import SpeechProviderError
from app.communication.voice.speech_router import (
    MeteredSTT, MeteredTTS, SpeechRouter, local_tts_from_env, speech_router_from_env,
)
from app.llm.router import ai_context

KOKORO_RATE = 24000
REPLY = "Hello, I am Nexus. You have two pending assignments: Operating Systems, and Computer Networks."
KOKORO_ENV = ("CAMPUSNEXUS_LOCAL_TTS_PROVIDER", "CAMPUSNEXUS_KOKORO_MODEL_PATH", "CAMPUSNEXUS_KOKORO_VOICES_PATH",
              "CAMPUSNEXUS_KOKORO_VOICE", "CAMPUSNEXUS_KOKORO_SPEED", "CAMPUSNEXUS_KOKORO_LANG",
              "CAMPUSNEXUS_PIPER_PATH", "CAMPUSNEXUS_PIPER_MODEL_PATH")
# Read before the autouse fixture clears the environment (the opt-in real-model test only).
REAL_MODEL = os.environ.get("CAMPUSNEXUS_KOKORO_MODEL_PATH")
REAL_VOICES = os.environ.get("CAMPUSNEXUS_KOKORO_VOICES_PATH")


def tone(seconds: float = 1.0, hz: float = 440.0, amplitude: float = 0.5, rate: int = KOKORO_RATE) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amplitude * np.sin(2 * math.pi * hz * t)).astype(np.float32)


class FakeKokoro:
    """``kokoro_onnx.Kokoro``'s surface: a ``voices`` mapping and ``create(text, voice, speed, lang, ...)``."""

    def __init__(self, samples=None, error: Exception | None = None) -> None:
        self.voices = {"af_heart": object(), "am_adam": object()}
        self.samples = tone() if samples is None else samples
        self.error, self.calls = error, []

    def create(self, text, voice, speed=1.0, lang="en-us", **kwargs):
        self.calls.append(dict(text=text, voice=voice, speed=speed, lang=lang, **kwargs))
        if self.error is not None:
            raise self.error
        return self.samples, KOKORO_RATE


class Loader:
    def __init__(self, engine=None, error: Exception | None = None) -> None:
        self.engine, self.error, self.calls = engine or FakeKokoro(), error, []

    def __call__(self, model, voices):
        self.calls.append((model, voices))
        if self.error is not None:
            raise self.error
        return self.engine


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in KOKORO_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(local_speech, "kokoro_installed", lambda: True)


@pytest.fixture
def files(tmp_path):
    out = {}
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin", "piper.exe", "voice.onnx"):
        (tmp_path / name).write_bytes(b"x")
        out[name] = tmp_path / name
    return out


@pytest.fixture
def kokoro_env(monkeypatch, files):
    monkeypatch.setenv("CAMPUSNEXUS_LOCAL_TTS_PROVIDER", "kokoro")
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_MODEL_PATH", str(files["kokoro-v1.0.onnx"]))
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_VOICES_PATH", str(files["voices-v1.0.bin"]))
    return files


@pytest.fixture
def no_sockets(monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return attempts


def provider(files, loader=None, **config) -> KokoroTTSProvider:
    return KokoroTTSProvider(KokoroConfig(files["kokoro-v1.0.onnx"], files["voices-v1.0.bin"], **config),
                             loader=loader or Loader())


# --- configuration ------------------------------------------------------------------------------------------------


def test_defaults_are_af_heart_at_0_88_in_en_us(kokoro_env) -> None:
    config = KokoroConfig.from_env()
    assert (config.voice, config.speed, config.language) == ("af_heart", 0.88, "en-us")
    assert config.model == kokoro_env["kokoro-v1.0.onnx"] and config.unavailable_reason() is None


def test_configured_voice_and_speed_reach_the_engine_with_punctuation_pauses(kokoro_env, monkeypatch, no_sockets) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_VOICE", "am_adam")
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_SPEED", "0.95")
    loader = Loader()
    tts = KokoroTTSProvider(KokoroConfig.from_env(), loader=loader)
    tts.synthesize(REPLY)
    (call,) = loader.engine.calls
    assert (call["voice"], call["speed"], call["lang"]) == ("am_adam", 0.95, "en-us")
    assert call["text"] == REPLY
    assert call["sentence_pause"] > call["clause_pause"] > 0
    assert no_sockets == []


@pytest.mark.parametrize("speed", ["0.75", "1.15", "0.88", "1"])
def test_speed_inside_the_bounds_is_accepted(kokoro_env, monkeypatch, speed) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_SPEED", speed)
    assert KokoroConfig.from_env().speed == float(speed)


@pytest.mark.parametrize("speed", ["0.74", "1.16", "0.5", "2.0", "nan", "inf", "-1", "fast"])
def test_speed_outside_the_bounds_is_a_configuration_error(kokoro_env, monkeypatch, speed) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_SPEED", speed)
    with pytest.raises(ValueError):
        KokoroConfig.from_env()
    with pytest.raises(ValueError):  # the app reports INVALID_SPEECH_CONFIG instead of starting a bad voice
        local_tts_from_env()


def test_speed_bounds_hold_for_a_directly_built_config(files) -> None:
    for speed in (0.7, 1.2):
        with pytest.raises(ValueError):
            KokoroConfig(files["kokoro-v1.0.onnx"], files["voices-v1.0.bin"], speed=speed)


@pytest.mark.parametrize("name, value", [
    ("CAMPUSNEXUS_KOKORO_VOICE", "../../voices"), ("CAMPUSNEXUS_KOKORO_VOICE", "af_heart --model x"),
    ("CAMPUSNEXUS_KOKORO_LANG", "fr-fr"), ("CAMPUSNEXUS_KOKORO_LANG", "en-us; rm"),
])
def test_voice_and_language_are_validated(kokoro_env, monkeypatch, name, value) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        KokoroConfig.from_env()


def test_a_voice_missing_from_the_voices_file_is_a_code(files) -> None:
    tts = provider(files, voice="bf_emma")
    with pytest.raises(LocalToolError) as exc:
        tts.synthesize(REPLY)
    assert exc.value.code == "KOKORO_VOICE_NOT_FOUND"


# --- privacy of paths ---------------------------------------------------------------------------------------------


def test_paths_must_be_absolute_existing_files_from_the_environment(kokoro_env, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_MODEL_PATH", "kokoro-v1.0.onnx")  # relative: never resolved
    assert KokoroConfig.from_env().unavailable_reason() == "KOKORO_MODEL_NOT_CONFIGURED"
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_MODEL_PATH", str(kokoro_env["kokoro-v1.0.onnx"]))
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_VOICES_PATH", str(tmp_path / "missing.bin"))
    assert KokoroConfig.from_env().unavailable_reason() == "KOKORO_VOICES_NOT_CONFIGURED"
    monkeypatch.setattr(local_speech, "kokoro_installed", lambda: False)
    monkeypatch.setenv("CAMPUSNEXUS_KOKORO_VOICES_PATH", str(kokoro_env["voices-v1.0.bin"]))
    assert KokoroConfig.from_env().unavailable_reason() == "KOKORO_NOT_INSTALLED"


def test_load_and_synthesis_failures_never_expose_paths_or_text(files) -> None:
    secret = str(files["kokoro-v1.0.onnx"])
    tts = provider(files, loader=Loader(error=FileNotFoundError(f"Model file not found at {secret}")))
    with pytest.raises(LocalToolError) as exc:
        tts.synthesize(REPLY)
    assert exc.value.code == "KOKORO_LOAD_FAILED" and exc.value.__cause__ is None
    assert secret not in str(exc.value) and secret not in repr(exc.value)

    tts = provider(files, loader=Loader(FakeKokoro(error=ValueError(f"No phonemes of {REPLY!r}"))))
    with pytest.raises(LocalToolError) as exc:
        tts.synthesize(REPLY)
    assert exc.value.code == "LOCAL_SPEECH_FAILED" and exc.value.__cause__ is None
    assert "Nexus" not in str(exc.value) and "Nexus" not in repr(exc.value)


def test_health_reports_kokoro_and_readiness_without_paths(kokoro_env, monkeypatch) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_SPEECH_MODE", "local")
    stt = SimpleNamespace(provider_name="whisper_cpp", available=lambda: True, transcribe=None)
    tts = local_tts_from_env()
    router = SpeechRouter(stt_mode="local", tts_mode="local", connectivity=SimpleNamespace(),
                          local_stt=MeteredSTT(stt, "whisper_cpp", "whisper.cpp"), local_tts=tts)
    status = router.status()
    assert status["tts_provider"] == "kokoro" and status["local_ready"] is True
    rendered = repr(status)
    assert str(kokoro_env["kokoro-v1.0.onnx"]) not in rendered and "voices-v1.0" not in rendered
    assert tts.model_name == "kokoro-onnx"  # telemetry names the engine, never the model file

    monkeypatch.setattr(local_speech, "kokoro_installed", lambda: False)
    status = router.status()
    assert status["tts_provider"] is None and status["local_ready"] is False


# --- selection: Kokoro preferred, Piper explicit, never a silent fallback -----------------------------------------


def test_provider_selection(kokoro_env, monkeypatch) -> None:
    assert isinstance(local_tts_from_env().inner, KokoroTTSProvider)
    monkeypatch.setenv("CAMPUSNEXUS_LOCAL_TTS_PROVIDER", "piper")
    assert isinstance(local_tts_from_env().inner, PiperTTSProvider)
    monkeypatch.delenv("CAMPUSNEXUS_LOCAL_TTS_PROVIDER")
    assert isinstance(local_tts_from_env().inner, PiperTTSProvider)  # unset keeps the Phase 6 behaviour
    monkeypatch.setenv("CAMPUSNEXUS_LOCAL_TTS_PROVIDER", "espeak")
    with pytest.raises(ValueError):
        local_tts_from_env()


def test_configured_kokoro_that_is_unavailable_never_falls_back_to_piper(monkeypatch, files) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_SPEECH_MODE", "local")
    monkeypatch.setenv("CAMPUSNEXUS_LOCAL_TTS_PROVIDER", "kokoro")  # but no Kokoro paths
    monkeypatch.setenv("CAMPUSNEXUS_PIPER_PATH", str(files["piper.exe"]))
    monkeypatch.setenv("CAMPUSNEXUS_PIPER_MODEL_PATH", str(files["voice.onnx"]))
    router = speech_router_from_env(SimpleNamespace(state=lambda: None, invalidate=lambda: None))
    assert router.local_tts.provider_name == "kokoro" and router.status()["tts_provider"] is None
    with pytest.raises(SpeechProviderError) as exc:
        router.synthesize(REPLY)
    assert exc.value.code == "LOCAL_TTS_UNAVAILABLE"


# --- output format and reuse --------------------------------------------------------------------------------------


def test_output_is_normalized_to_16_khz_mono_linear16(files, no_sockets) -> None:
    pcm = provider(files).synthesize(REPLY)
    assert abs(len(pcm) - BYTES_PER_SECOND) <= 4  # 1 s at 24 kHz -> 1 s at 16 kHz, 16-bit
    assert wav_to_pcm(pcm_to_wav(pcm), max_bytes=10 ** 6) == pcm  # exactly the internal WAV format
    assert no_sockets == []


def test_conversion_clips_bounds_and_rejects_bad_audio() -> None:
    loud = kokoro_samples_to_pcm(np.full(KOKORO_RATE, 4.0, dtype=np.float32), KOKORO_RATE)
    assert max(np.frombuffer(loud, dtype="<i2")) <= 32767
    for samples, rate, code in [
        (np.zeros(0, dtype=np.float32), KOKORO_RATE, "TTS_EMPTY_AUDIO"),
        (np.zeros(KOKORO_RATE * 46, dtype=np.float32), KOKORO_RATE, "TTS_AUDIO_TOO_LONG"),
        (tone(), 1000, "TTS_BAD_AUDIO"),
    ]:
        with pytest.raises(LocalToolError) as exc:
            kokoro_samples_to_pcm(samples, rate)
        assert exc.value.code == code


def test_content_above_8_khz_is_filtered_before_decimation() -> None:
    def rms(pcm: bytes) -> float:
        data = np.frombuffer(pcm, dtype="<i2").astype(np.float64)[200:-200]
        return float(np.sqrt(np.mean(data ** 2)))

    speech_band = rms(kokoro_samples_to_pcm(tone(hz=1000), KOKORO_RATE))
    aliasing_band = rms(kokoro_samples_to_pcm(tone(hz=10000), KOKORO_RATE))
    assert speech_band > 10000 and aliasing_band < speech_band / 20


def test_the_engine_is_loaded_once_and_reused(files) -> None:
    loader = Loader()
    tts = provider(files, loader=loader)
    for _ in range(3):
        tts.synthesize(REPLY)
    assert len(loader.calls) == 1 and len(loader.engine.calls) == 3


def test_text_is_bounded_and_cleaned_like_piper(files) -> None:
    loader = Loader()
    tts = provider(files, loader=loader)
    tts.synthesize("Hello\x00\x07   Nexus.\n")
    assert loader.engine.calls[0]["text"] == "Hello Nexus."
    for text, code in [("x" * 401, "TTS_INPUT_TOO_LONG"), (" \x00 ", "TTS_EMPTY_INPUT")]:
        with pytest.raises(LocalToolError) as exc:
            tts.synthesize(text)
        assert exc.value.code == code
    assert len(loader.engine.calls) == 1


# --- nothing persisted or logged ----------------------------------------------------------------------------------


def test_no_text_or_audio_is_written_logged_or_recorded(files, monkeypatch, tmp_path, caplog, no_sockets) -> None:
    def no_files(*args, **kwargs):
        raise AssertionError("a temp file was created")

    monkeypatch.setattr(tempfile, "mkstemp", no_files)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", no_files)
    monkeypatch.chdir(tmp_path)
    before = set(os.listdir(tmp_path))
    caplog.set_level(logging.DEBUG)
    tts = MeteredTTS(provider(files), "kokoro", "kokoro-onnx")
    with ai_context(None) as ai:
        pcm = tts.synthesize(REPLY)
    assert set(os.listdir(tmp_path)) == before
    assert "Nexus" not in caplog.text and "assignments" not in caplog.text
    (record,) = ai.records
    assert (record.operation, record.provider, record.model, record.success) == ("voice_tts", "kokoro", "kokoro-onnx", True)
    assert record.audio_ms == len(pcm) * 1000 // BYTES_PER_SECOND
    assert "Nexus" not in repr(record)


def test_the_real_loader_quiets_kokoros_debug_log(monkeypatch, files) -> None:
    calls = []
    fake_module = SimpleNamespace(Kokoro=lambda model, voices: calls.append((model, voices)) or FakeKokoro())
    monkeypatch.setitem(__import__("sys").modules, "kokoro_onnx", fake_module)
    package_log = logging.getLogger("kokoro_onnx")
    monkeypatch.setattr(package_log, "level", logging.DEBUG)
    local_speech.load_kokoro(files["kokoro-v1.0.onnx"], files["voices-v1.0.bin"])
    assert package_log.getEffectiveLevel() >= logging.WARNING
    assert calls == [(str(files["kokoro-v1.0.onnx"]), str(files["voices-v1.0.bin"]))]


# --- the real model (opt-in) --------------------------------------------------------------------------------------


@pytest.mark.skipif(not (REAL_MODEL and REAL_VOICES), reason="Kokoro model/voices not configured")
def test_real_kokoro_synthesizes_offline() -> None:
    pytest.importorskip("kokoro_onnx")
    tts = KokoroTTSProvider(KokoroConfig(local_speech.Path(REAL_MODEL), local_speech.Path(REAL_VOICES)))
    original = socket.socket.connect
    socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
    try:
        pcm = tts.synthesize("Your attendance in Operating Systems is eighty two percent.")
    finally:
        socket.socket.connect = original
    assert len(pcm) > SAMPLE_RATE and wav_to_pcm(pcm_to_wav(pcm), max_bytes=10 ** 7) == pcm
