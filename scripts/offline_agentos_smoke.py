"""Offline AgentOS smoke check (Phase 6): can this edge deployment run Nexus with no internet?

Checks, in order (each prints a safe status, never a URL, path, prompt or reply):

1. the database is local SQLite (Edge mode; ``CAMPUSNEXUS_DATABASE_MODE=local`` or unset);
2. the local Ollama endpoint is reachable and the configured model (``CAMPUSNEXUS_OLLAMA_MODEL``, default
   ``gpt-oss:20b``) is installed -- nothing is ever pulled;
3. local STT (whisper.cpp) and local TTS (Piper) are ready when configured.

With ``--run-nexus`` it then sends one Nexus request ("Who am I and what can you help me with?") as the given
account (default ``student@campusnexus.local``) through the normal Agent Kernel with
``CAMPUSNEXUS_INTELLIGENCE_MODE=local`` and the developer offline mode (``CAMPUSNEXUS_OFFLINE_MODE=1``: every cloud
provider is refused) plus a socket guard that blocks any non-loopback connection, and prints provider, model,
mission status, steps and ``cloud_calls``. The request creates a normal mission in the configured local database.

Exit codes: 0 ready (and the Nexus run completed), 1 a check failed, 3 SKIPPED (Ollama or the model unavailable).

    python scripts/offline_agentos_smoke.py
    python scripts/offline_agentos_smoke.py --run-nexus [--account student@campusnexus.local] [--show-text]
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

QUESTION = "Who am I and what can you help me with?"
SKIPPED = 3


def _socket_guard() -> list:
    """Refuse every non-loopback connection from this process; returns the list of refused attempts."""
    refused: list = []
    original = socket.socket.connect

    def connect(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        try:
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            refused.append("non_loopback")
            raise OSError("offline smoke: non-loopback connection refused")
        return original(self, address)

    socket.socket.connect = connect  # type: ignore[method-assign]
    return refused


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-nexus", action="store_true", help="send one Nexus request through the local brain")
    parser.add_argument("--account", default="student@campusnexus.local", help="the account the request runs as")
    parser.add_argument("--show-text", action="store_true", help="also print the reply text (off by default)")
    args = parser.parse_args(argv)

    # Developer offline test mode: decided before anything is built.
    os.environ["CAMPUSNEXUS_OFFLINE_MODE"] = "1"
    os.environ["CAMPUSNEXUS_INTELLIGENCE_MODE"] = "local"
    refused = _socket_guard()

    from app.agentos.brain import BrainUnavailableError
    from app.agentos.local_brain import LocalEndpointError, OllamaAgentBrain, OllamaConfig, model_installed
    from app.communication.voice.local_speech import PiperConfig, WhisperConfig
    from app.db.session import database_mode, describe_database, open_database

    report: dict = {"offline_mode": True}
    try:
        engine = open_database()
        dialect = describe_database(engine)["dialect"]
    except Exception as exc:  # noqa: BLE001 -- report the kind only (a message could name a host)
        print(json.dumps({**report, "database": "unavailable", "error": type(exc).__name__}))
        return 1
    report["database"] = dialect
    if dialect != "sqlite" or database_mode() == "postgres":
        print(json.dumps({**report, "result": "FAILED", "reason": "EDGE_MODE_REQUIRES_LOCAL_SQLITE"}))
        return 1

    try:
        config = OllamaConfig.from_env()
    except (LocalEndpointError, ValueError) as exc:
        print(json.dumps({**report, "result": "FAILED", "reason": str(exc) or "INVALID_LOCAL_BRAIN_CONFIG"}))
        return 1
    brain = OllamaAgentBrain(config)
    report.update(provider="ollama", model=config.model)
    try:
        installed = brain.available_models()
    except BrainUnavailableError as exc:
        print(json.dumps({**report, "result": "SKIPPED", "reason": exc.code}))
        return SKIPPED
    if not model_installed(installed, config.model):
        print(json.dumps({**report, "result": "SKIPPED", "reason": "LOCAL_MODEL_UNAVAILABLE"}))
        return SKIPPED
    report["ollama"] = "ready"

    whisper, piper = WhisperConfig.from_env(), PiperConfig.from_env()
    report["local_stt"] = "ready" if whisper.unavailable_reason() is None else whisper.unavailable_reason()
    report["local_tts"] = "ready" if piper.unavailable_reason() is None else piper.unavailable_reason()

    if not args.run_nexus:
        print(json.dumps({**report, "result": "READY"}))
        return 0
    return _run_nexus(engine, args, report, refused)


def _run_nexus(engine, args, report: dict, refused: list) -> int:
    from sqlalchemy import select

    from app.agentos.bootstrap import build_agent_runtime
    from app.agentos.brain_router import build_configured_brain
    from app.agentos.connectivity import connectivity_from_env
    from app.agentos.nexus import PersonalAssistant
    from app.agentos.runtime import MissionActor
    from app.db.models.agent_kernel import AgentStep
    from app.db.models.ai_usage import AIUsageEvent
    from app.db.models.auth import AuthAccount
    from app.db.models.identity import Student
    from app.db.models.organization import OrganizationMembership
    from app.db.system_session import open_system_session
    from app.db.tenant_session import TenantSessionFactory
    from app.schemas.enums import MembershipStatus
    from app.services.ai_usage import AIUsageRecorder

    with open_system_session(engine) as system:  # identity lookup only (a maintenance script)
        account = system.execute(select(AuthAccount).where(AuthAccount.email == args.account.strip().lower())).scalars().first()
        membership = None if account is None else system.execute(select(OrganizationMembership).where(
            OrganizationMembership.account_id == account.id,
            OrganizationMembership.status == MembershipStatus.ACTIVE)).scalars().first()
        if account is None or membership is None:
            print(json.dumps({**report, "result": "FAILED", "reason": "ACCOUNT_NOT_FOUND"}))
            return 1
        student = system.get(Student, membership.student_id) if membership.student_id is not None else None
        actor = MissionActor(organization_id=membership.organization_id, account_id=account.id, role=membership.role,
                             student_code=student.student_code if student is not None else None,
                             faculty_profile_id=membership.faculty_profile_id)

    factory = TenantSessionFactory(engine)
    recorder = AIUsageRecorder(factory)
    connectivity = connectivity_from_env()
    brain = build_configured_brain(recorder=recorder, connectivity=connectivity)
    runtime = build_agent_runtime(brain, connectivity=connectivity)
    with factory.open_tenant_session(actor.organization_id) as session:
        try:
            reply = PersonalAssistant(runtime).handle_message(session, actor, QUESTION, recorder=recorder)
        except Exception as exc:  # noqa: BLE001 -- AssistantUnavailable carries a safe code
            print(json.dumps({**report, "result": "FAILED", "reason": getattr(exc, "code", type(exc).__name__),
                              "cloud_calls": len(refused)}))
            return 1
        steps = list(session.execute(select(AgentStep.action_type, AgentStep.tool_name).where(
            AgentStep.mission_id == reply.mission_id).order_by(AgentStep.step_number)).all())
    with factory.open_tenant_session(actor.organization_id) as session:
        providers = list(session.execute(select(AIUsageEvent.provider).where(
            AIUsageEvent.mission_id == f"agentos:{reply.mission_id}")).scalars())
    cloud_calls = len(refused) + sum(1 for p in providers if p not in ("ollama", None))
    out = {**report, "provider": reply.brain.provider, "model": reply.brain.model, "mission_id": reply.mission_id,
           "mission_status": reply.status.value, "steps": [f"{a}:{t}" if t else a for a, t in steps],
           "cloud_calls": cloud_calls,
           "result": "PASSED" if reply.status.value == "completed" and cloud_calls == 0 else "FAILED"}
    if args.show_text:
        out["assistant_message"] = reply.assistant_message
    print(json.dumps(out))
    return 0 if out["result"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
