"""Local Groq speech smoke check (AgentOS V2 Phase 5.1). Real API calls -- never run by pytest.

    python scripts/groq_voice_pipeline_smoke.py
    python scripts/groq_voice_pipeline_smoke.py --transcribe path/to/test_16k_mono.wav
    python scripts/groq_voice_pipeline_smoke.py --out data/demo/tts_check.wav

Without ``GROQ_API_KEY`` it prints SKIPPED and exits 0. Otherwise it synthesizes one short phrase with the configured
TTS model/voice, validates that the result converts to linear16 mono 16 kHz PCM, and -- only if ``--transcribe`` is
given -- transcribes that local WAV (which must already be 16 kHz mono 16-bit). It prints only models, formats,
sizes, latencies and status codes: never the transcript, the audio or the key. Provider output is written to disk
only when ``--out`` is passed explicitly.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.communication.voice.audio import (  # noqa: E402
    BYTES_PER_SECOND, SAMPLE_RATE, AudioFormatError, duration_seconds, pcm_to_wav, wav_to_pcm,
)
from app.communication.voice.speech import (  # noqa: E402
    GroqHTTP, GroqSpeechToText, GroqTextToSpeech, SpeechProviderError, VoiceProviderConfig,
)

PHRASE = "Hello. This is CampusNexus checking that voice synthesis works."
MAX_INPUT_SECONDS = 15


def _print(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transcribe", type=Path, help="a local 16 kHz mono 16-bit WAV to transcribe (optional)")
    parser.add_argument("--out", type=Path, help="write the synthesized audio here as WAV (optional)")
    args = parser.parse_args(argv)

    config = VoiceProviderConfig.from_env()
    if not config.api_key:
        _print({"status": "SKIPPED", "reason": "GROQ_API_KEY_MISSING"})
        return 0
    reason = config.unavailable_reason()
    if reason is not None:
        _print({"status": "UNAVAILABLE", "reason": reason})
        return 2
    http = GroqHTTP(config.api_key, timeout=config.timeout)
    report: dict = {"status": "OK", "tts_model": config.tts_model, "tts_voice": config.tts_voice,
                    "format": f"linear16/mono/{SAMPLE_RATE}Hz"}

    started = time.perf_counter()
    try:
        pcm = GroqTextToSpeech(http, config.tts_model, config.tts_voice).synthesize(PHRASE)
    except SpeechProviderError as exc:
        _print({**report, "status": "FAILED", "stage": "tts", "code": exc.code})
        return 1
    report.update(tts_ms=int((time.perf_counter() - started) * 1000), tts_pcm_bytes=len(pcm),
                  tts_seconds=round(duration_seconds(len(pcm)), 2), tts_format_valid=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(pcm_to_wav(pcm))
        report["tts_written"] = True

    if args.transcribe:
        try:
            utterance = wav_to_pcm(args.transcribe.read_bytes(), max_bytes=MAX_INPUT_SECONDS * BYTES_PER_SECOND)
        except (OSError, AudioFormatError) as exc:
            _print({**report, "status": "FAILED", "stage": "input_wav", "code": str(exc)[:64]})
            return 1
        started = time.perf_counter()
        try:
            transcript = GroqSpeechToText(http, config.stt_model, config.language).transcribe(utterance)
        except SpeechProviderError as exc:
            _print({**report, "status": "FAILED", "stage": "stt", "code": exc.code})
            return 1
        report.update(stt_model=config.stt_model, stt_ms=int((time.perf_counter() - started) * 1000),
                      stt_chars=len(transcript.text), stt_usable=transcript.usable)  # never the words themselves
    _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
