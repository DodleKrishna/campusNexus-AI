"""Offline voice smoke check (Phase 6.2): one real WAV through the production Nexus voice path with no internet.

real WAV -> ``SpeechRouter`` (local whisper.cpp) -> ``VoiceAssistant`` / ``PersonalAssistant`` (one Nexus mission)
-> the configured brain (``OllamaAgentBrain``) -> the normal ``AgentRuntime`` tool execution -> ``SpeechRouter``
(local Kokoro or Piper) -> WAV reply. These are the same objects ``POST /agentos/assistant/voice`` uses
(``speech_router_from_env``, ``build_configured_brain``, ``build_agent_runtime``, ``VoiceAssistant``).

The environment must already be offline and local; nothing is switched on here, and nothing is faked:

    CAMPUSNEXUS_OFFLINE_MODE=1  CAMPUSNEXUS_INTELLIGENCE_MODE=local  CAMPUSNEXUS_SPEECH_MODE=local
    CAMPUSNEXUS_TTS_MODE=local (or unset)  CAMPUSNEXUS_DATABASE_MODE=local (or unset, with SQLite)
    plus the whisper.cpp / Kokoro or Piper / Ollama variables (see .env.example).

A socket guard refuses every non-loopback connection. The request runs as the given account (default
``student@campusnexus.local``) and creates a normal mission in the local database. The transcript and the reply are
printed only with ``--show-text``; the reply audio is written only to an explicit ``--out`` path.

Exit codes: 0 PASSED, 1 FAILED, 2 the environment is not offline/local.

    python scripts/offline_voice_smoke.py --wav question.wav [--account student@campusnexus.local] [--show-text]
                                          [--out reply.wav]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.offline_agentos_smoke import _socket_guard  # noqa: E402

NOT_OFFLINE = 2
LOCAL_PROVIDERS = frozenset({"ollama", "whisper_cpp", "kokoro", "piper"})
LOCAL_TTS_PROVIDERS = ("kokoro", "piper")
VOICE_OPERATIONS = ("voice_stt", "voice_tts")


def offline_env_problem(environ: Mapping[str, str]) -> Optional[str]:
    """The first reason this environment is not a pure offline/local voice setup, else None."""
    def value(name: str) -> str:
        return (environ.get(name) or "").strip().lower()

    if value("CAMPUSNEXUS_OFFLINE_MODE") != "1":
        return "OFFLINE_MODE_REQUIRED"
    if value("CAMPUSNEXUS_INTELLIGENCE_MODE") != "local":
        return "LOCAL_INTELLIGENCE_REQUIRED"
    if value("CAMPUSNEXUS_SPEECH_MODE") != "local" or value("CAMPUSNEXUS_TTS_MODE") not in ("", "local"):
        return "LOCAL_SPEECH_REQUIRED"
    if value("CAMPUSNEXUS_DATABASE_MODE") not in ("", "local"):
        return "EDGE_MODE_REQUIRES_LOCAL_SQLITE"
    return None


def wav_audio_ms(wav_base64: Optional[str]) -> int:
    """Duration of a 16 kHz mono 16-bit WAV reply (0 when there is none)."""
    if not wav_base64:
        return 0
    from app.communication.voice.audio import BYTES_PER_SECOND, upload_wav_to_pcm

    pcm = upload_wav_to_pcm(base64.b64decode(wav_base64), max_seconds=3600)
    return int(len(pcm) * 1000 / BYTES_PER_SECOND)


def count_cloud_calls(providers: Iterable[Optional[str]], refused: list) -> int:
    """Refused non-loopback connections plus any recorded AI/speech call by a non-local provider."""
    return len(refused) + sum(1 for p in providers if p is not None and p not in LOCAL_PROVIDERS)


def _actor(engine, email: str):
    from sqlalchemy import select

    from app.agentos.runtime import MissionActor
    from app.db.models.auth import AuthAccount
    from app.db.models.identity import Student
    from app.db.models.organization import OrganizationMembership
    from app.db.system_session import open_system_session
    from app.schemas.enums import MembershipStatus

    with open_system_session(engine) as system:  # identity lookup only (a maintenance script)
        account = system.execute(select(AuthAccount).where(AuthAccount.email == email.strip().lower())).scalars().first()
        membership = None if account is None else system.execute(select(OrganizationMembership).where(
            OrganizationMembership.account_id == account.id,
            OrganizationMembership.status == MembershipStatus.ACTIVE)).scalars().first()
        if account is None or membership is None:
            return None
        student = system.get(Student, membership.student_id) if membership.student_id is not None else None
        return MissionActor(organization_id=membership.organization_id, account_id=account.id, role=membership.role,
                            student_code=student.student_code if student is not None else None,
                            faculty_profile_id=membership.faculty_profile_id)


class _TextTap:
    """Keeps the transcript in memory for ``--show-text`` only; otherwise the router is used unwrapped."""

    def __init__(self, router) -> None:
        self.router, self.transcript = router, None

    def transcribe(self, pcm: bytes):
        transcript, provider = self.router.transcribe(pcm)
        self.transcript = transcript.text
        return transcript, provider

    def synthesize(self, text: str):
        return self.router.synthesize(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wav", required=True, type=Path, help="16 kHz mono 16-bit WAV utterance (at most 30 s)")
    parser.add_argument("--account", default="student@campusnexus.local", help="the account the request runs as")
    parser.add_argument("--show-text", action="store_true", help="also print the transcript and the reply text")
    parser.add_argument("--out", type=Path, help="write the spoken reply WAV here (nothing is written otherwise)")
    args = parser.parse_args(argv)

    problem = offline_env_problem(os.environ)
    if problem is not None:
        print(json.dumps({"result": "FAILED", "reason": problem}))
        return NOT_OFFLINE
    refused = _socket_guard()

    from sqlalchemy import or_, select

    from app.agentos.bootstrap import build_agent_runtime
    from app.agentos.brain_router import build_configured_brain
    from app.agentos.connectivity import connectivity_from_env
    from app.agentos.nexus import AssistantUnavailable, PersonalAssistant
    from app.agentos.runtime import AgentOSError
    from app.agentos.voice_assistant import VoiceAssistant
    from app.communication.voice.speech_router import speech_router_from_env
    from app.db.models.agent_kernel import AgentStep
    from app.db.models.ai_usage import AIUsageEvent
    from app.db.session import database_mode, describe_database, open_database
    from app.db.tenant_session import TenantSessionFactory
    from app.services.ai_usage import AIUsageRecorder

    report: dict = {}
    try:
        engine = open_database()
        report["database"] = describe_database(engine)["dialect"]
    except Exception as exc:  # noqa: BLE001 -- report the kind only (a message could name a host)
        print(json.dumps({"database": "unavailable", "result": "FAILED", "reason": type(exc).__name__}))
        return 1
    if report["database"] != "sqlite" or database_mode() == "postgres":
        print(json.dumps({**report, "result": "FAILED", "reason": "EDGE_MODE_REQUIRES_LOCAL_SQLITE"}))
        return NOT_OFFLINE
    try:
        wav = args.wav.read_bytes()
    except OSError:
        print(json.dumps({**report, "result": "FAILED", "reason": "WAV_NOT_READABLE"}))
        return 1
    actor = _actor(engine, args.account)
    if actor is None:
        print(json.dumps({**report, "result": "FAILED", "reason": "ACCOUNT_NOT_FOUND"}))
        return 1

    factory = TenantSessionFactory(engine)
    recorder = AIUsageRecorder(factory)
    connectivity = connectivity_from_env()
    brain = build_configured_brain(recorder=recorder, connectivity=connectivity)
    runtime = build_agent_runtime(brain, connectivity=connectivity)
    try:
        router = speech_router_from_env(connectivity)
    except ValueError:
        print(json.dumps({**report, "result": "FAILED", "reason": "INVALID_SPEECH_CONFIG"}))
        return 1
    speech = _TextTap(router) if args.show_text else router

    started = datetime.now(timezone.utc)
    with factory.open_tenant_session(actor.organization_id) as session:
        try:
            reply = VoiceAssistant(PersonalAssistant(runtime), speech).handle(session, actor, wav, recorder=recorder)
        except (AgentOSError, AssistantUnavailable) as exc:
            out = {**report, "result": "FAILED", "reason": exc.code, "cloud_calls": len(refused)}
            mission = getattr(getattr(exc, "reply", None), "mission_id", None)
            if mission is not None:
                out["mission_id"] = mission
            print(json.dumps(out))
            return 1
        steps = list(session.execute(select(AgentStep.action_type, AgentStep.tool_name).where(
            AgentStep.mission_id == reply.mission_id).order_by(AgentStep.step_number)).all())
    with factory.open_tenant_session(actor.organization_id) as session:
        providers = list(session.execute(select(AIUsageEvent.provider).where(or_(
            AIUsageEvent.mission_id == f"agentos:{reply.mission_id}",
            (AIUsageEvent.operation.in_(VOICE_OPERATIONS)) & (AIUsageEvent.created_at >= started)))).scalars())

    cloud_calls = count_cloud_calls(providers, refused)
    output_audio_ms = wav_audio_ms(reply.audio_wav_base64)
    passed = (reply.status.value == "completed" and cloud_calls == 0 and reply.speech.stt_provider == "whisper_cpp"
              and reply.brain.provider == "ollama" and reply.speech.tts_provider in LOCAL_TTS_PROVIDERS
              and output_audio_ms > 0)
    out = {**report, "stt_provider": reply.speech.stt_provider, "brain_provider": reply.brain.provider,
           "brain_model": reply.brain.model, "tts_provider": reply.speech.tts_provider,
           "mission_id": reply.mission_id, "mission_status": reply.status.value,
           "steps": [f"{a}:{t}" if t else a for a, t in steps], "output_audio_ms": output_audio_ms,
           "cloud_calls": cloud_calls, "result": "PASSED" if passed else "FAILED"}
    if reply.audio_error_code:
        out["audio_error_code"] = reply.audio_error_code
    if args.show_text:
        out["transcript"] = speech.transcript
        out["assistant_message"] = reply.assistant_message
    if args.out is not None and reply.audio_wav_base64:
        args.out.write_bytes(base64.b64decode(reply.audio_wav_base64))
    print(json.dumps(out))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
