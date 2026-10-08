"""scripts/offline_voice_smoke.py: the offline/local environment gate and the result helpers (no binaries, no network)."""
from __future__ import annotations

import base64
import json

import pytest

from app.communication.voice.audio import pcm_to_wav
from scripts import offline_voice_smoke as smoke

OFFLINE = {"CAMPUSNEXUS_OFFLINE_MODE": "1", "CAMPUSNEXUS_INTELLIGENCE_MODE": "local",
           "CAMPUSNEXUS_SPEECH_MODE": "local"}


def test_pure_offline_local_environment_is_accepted() -> None:
    assert smoke.offline_env_problem(OFFLINE) is None
    assert smoke.offline_env_problem({**OFFLINE, "CAMPUSNEXUS_TTS_MODE": "local",
                                      "CAMPUSNEXUS_DATABASE_MODE": "local"}) is None


@pytest.mark.parametrize("override, reason", [
    ({"CAMPUSNEXUS_OFFLINE_MODE": ""}, "OFFLINE_MODE_REQUIRED"),
    ({"CAMPUSNEXUS_OFFLINE_MODE": "0"}, "OFFLINE_MODE_REQUIRED"),
    ({"CAMPUSNEXUS_INTELLIGENCE_MODE": "auto"}, "LOCAL_INTELLIGENCE_REQUIRED"),
    ({"CAMPUSNEXUS_INTELLIGENCE_MODE": ""}, "LOCAL_INTELLIGENCE_REQUIRED"),
    ({"CAMPUSNEXUS_SPEECH_MODE": "auto"}, "LOCAL_SPEECH_REQUIRED"),
    ({"CAMPUSNEXUS_TTS_MODE": "cloud"}, "LOCAL_SPEECH_REQUIRED"),
    ({"CAMPUSNEXUS_DATABASE_MODE": "postgres"}, "EDGE_MODE_REQUIRES_LOCAL_SQLITE"),
])
def test_any_cloud_or_auto_setting_is_refused(override: dict, reason: str) -> None:
    assert smoke.offline_env_problem({**OFFLINE, **override}) == reason


def test_cloud_calls_count_refused_sockets_and_non_local_providers() -> None:
    assert smoke.count_cloud_calls(["ollama", "whisper_cpp", "piper", None], []) == 0
    assert smoke.count_cloud_calls(["ollama", "whisper_cpp", "kokoro"], []) == 0
    assert smoke.count_cloud_calls(["ollama", "groq"], []) == 1
    assert smoke.count_cloud_calls(["ollama"], ["non_loopback"]) == 1


def test_output_audio_ms_is_measured_from_the_reply_wav() -> None:
    assert smoke.wav_audio_ms(None) == 0
    one_and_half_seconds = b"\x00\x00" * 24000
    assert smoke.wav_audio_ms(base64.b64encode(pcm_to_wav(one_and_half_seconds)).decode("ascii")) == 1500


def test_main_refuses_before_building_anything_when_not_offline(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.delenv("CAMPUSNEXUS_OFFLINE_MODE", raising=False)
    assert smoke.main(["--wav", str(tmp_path / "missing.wav")]) == smoke.NOT_OFFLINE
    assert json.loads(capsys.readouterr().out) == {"result": "FAILED", "reason": "OFFLINE_MODE_REQUIRED"}
