"""AgentOS V2 Phase 5 (voice): Exotel connector, signed tokens, status callbacks, the WebSocket stream and the bounded
conversation. Offline: a fake ``ExotelClient`` and a fake speech/turn pipeline; no provider is ever called.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.websockets import WebSocketDisconnect

from app.communication.connectors import ConnectorRegistry
from app.communication.voice import tokens
from app.communication.voice.conversation import VoiceConversation, VoicePipeline, VoiceTurn
from app.communication.voice.exotel import ExotelConfig, ExotelError, ExotelVoiceConnector, ProviderCall
from app.db.models import (
    AgentMission, AgentMissionStatus, AssignmentFollowup, AttemptStatus, CommunicationAttempt, CommunicationJob,
    CommunicationJobStatus, CommunicationPreference, ContactKind, ContactPoint, DomainEvent, FollowupStatus,
    OperationAuditEvent, VoiceSession, VoiceSessionStatus,
)
from app.rules.communication_policy import CommunicationPolicy, VoiceOutcome
from app.rules.followup_policy import FollowupPolicy
from tests.test_agentos_assignment_guardian import T0, clock, mission_of, rows, small_class  # noqa: F401
from tests.test_agentos_communication import (
    NO_QUIET, PHONE, advance, comm, jobs, requested, tenant, use_runtime,
)
from tests.test_phase22b_tenant_isolation import app, client, orgs  # noqa: F401

CONFIG = ExotelConfig(account_sid="acct-test", api_key="key-test", api_token="token-test", caller_id="08000000000",
                      flow_app_id="4242", public_base_url="https://campusnexus.example")
VOICE_POLICY = CommunicationPolicy(max_attempts=2, backoff=timedelta(minutes=30), contact=FollowupPolicy(**NO_QUIET),
                                   voice_max_turns=3)
SPOKEN = "yes I will submit it tonight, call me on 99887 76655"


class FakeExotel:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list = []
        self.error = error

    def place_call(self, **kwargs) -> ProviderCall:
        if self.error:
            raise ExotelError(self.error)
        self.calls.append(kwargs)
        return ProviderCall(reference=f"CA{len(self.calls):04d}", status="queued")


class FakeSpeech:
    def transcribe(self, audio: bytes) -> str:
        return SPOKEN

    def synthesize(self, text: str) -> bytes:
        return text.encode("utf-8")


class ScriptedTurns:
    def __init__(self, *turns) -> None:
        self.turns, self.seen = list(turns), []

    def next_turn(self, context):
        self.seen.append(context)
        return self.turns.pop(0) if self.turns else VoiceTurn(reply="repeat")


def voice_setup(app, client, clock, small_class, session_factory, orgs, *, exotel=None, phone=True, verified=True):
    """GRD-001 consents to voice (preferred), has a phone number; the agent chooses voice; the job is READY."""
    exotel = exotel or FakeExotel()
    use_runtime(app, connectors=ConnectorRegistry([ExotelVoiceConnector(CONFIG, client=exotel, policy=VOICE_POLICY)]),
                policy=VOICE_POLICY)
    student = small_class["students"]["GRD-001"]
    with tenant(session_factory, orgs["a"]) as s:
        s.add(CommunicationPreference(student_id=student, allow_voice=True, preferred_channel="voice"))
        if phone:
            s.add(ContactPoint(student_id=student, kind=ContactKind.PHONE, address=PHONE, verified=verified))
        s.commit()
    requested(client, small_class, session_factory, orgs)
    comm(app, clock)
    (job,) = jobs(session_factory)
    advance(app, orgs["a"], job.agent_mission_id)
    (job,) = jobs(session_factory)
    assert (job.status, job.selected_channel) == (CommunicationJobStatus.READY, "voice")
    return job, exotel


def placed_call(app, client, clock, small_class, session_factory, orgs, **kwargs):
    job, exotel = voice_setup(app, client, clock, small_class, session_factory, orgs, **kwargs)
    assert [(r.outcome, r.code) for r in comm(app, clock).runs] == [("in_progress", None)]
    call = exotel.calls[0]
    callback_token = parse_qs(urlparse(call["status_callback"]).query)["token"][0]
    return job, call, callback_token


def status(client, token, sid, value, **extra):
    return client.post(f"/communication/exotel/status?token={token}", data={"CallSid": sid, "Status": value, **extra})


# --- Tokens and configuration ----------------------------------------------------------------------------------------


def test_voice_tokens_are_signed_scoped_and_expire() -> None:
    expires = T0 + timedelta(minutes=10)
    token = tokens.sign("stream", 7, 99, expires)
    claims = tokens.verify(token, "stream", T0)
    assert (claims.organization_id, claims.attempt_id) == (7, 99)
    with pytest.raises(tokens.VoiceTokenError, match="WRONG_PURPOSE"):
        tokens.verify(token, "callback", T0)
    with pytest.raises(tokens.VoiceTokenError, match="EXPIRED"):
        tokens.verify(token, "stream", expires)  # exactly at expiry: no longer valid
    body, mac = token.split(".")
    forged = base64.urlsafe_b64encode(b"stream.8.99." + str(int(expires.timestamp())).encode()).decode().rstrip("=")
    for bad in (f"{forged}.{mac}", f"{body}.{'0' * len(mac)}", "garbage", ""):
        with pytest.raises(tokens.VoiceTokenError):
            tokens.verify(bad, "stream", T0)


def test_exotel_config_is_masked_and_unavailable_until_complete() -> None:
    assert not ExotelConfig().configured() and CONFIG.configured()
    assert not ExotelConfig(**{**CONFIG.__dict__, "public_base_url": "http://insecure"}).configured()
    assert "token-test" not in repr(CONFIG) and "08000000000" not in repr(CONFIG) and "key-test" not in repr(CONFIG)
    assert ExotelVoiceConnector(ExotelConfig(), client=FakeExotel()).available() is False


# --- A full voice delivery -----------------------------------------------------------------------------------------------


def test_voice_call_stream_and_callback_end_to_end(client, small_class, app, clock, session_factory, orgs) -> None:
    job, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    assert call["to"] == PHONE and call["caller_id"] == CONFIG.caller_id and call["time_limit_seconds"] == 180
    (job,) = jobs(session_factory)
    (attempt,) = rows(session_factory, CommunicationAttempt)
    (voice,) = rows(session_factory, VoiceSession)
    assert (job.status, attempt.status, attempt.provider_reference) == (
        CommunicationJobStatus.IN_PROGRESS, AttemptStatus.IN_PROGRESS, "CA0001")
    assert (voice.status, voice.provider_call_reference, voice.max_turns) == (VoiceSessionStatus.PENDING, "CA0001", 3)

    brain = ScriptedTurns(VoiceTurn(reply="acknowledge", outcome=VoiceOutcome.WILL_SUBMIT, end_call=True))
    app.state.voice_pipeline = VoicePipeline(stt=FakeSpeech(), tts=FakeSpeech(), brain=brain)
    assert status(client, callback_token, "CA0001", "in-progress").json() == {"result": "updated"}
    with client.websocket_connect("/communication/voice/stream") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json({"event": "start", "start": {"stream_sid": "ST1", "custom_parameters": {"token": call["custom_field"]}}})
        greeting = base64.b64decode(ws.receive_json()["media"]["payload"]).decode()
        assert "Lab 3" in greeting and "GRD301" in greeting
        chunk = base64.b64encode(b"\x00" * 16000).decode()
        for _ in range(3):
            ws.send_json({"event": "media", "media": {"payload": chunk}})
        assert base64.b64decode(ws.receive_json()["media"]["payload"]).decode() == "Thank you. I have noted that."
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert brain.seen[0].utterance == SPOKEN and brain.seen[0].facts.title == "Lab 3"

    assert status(client, callback_token, "CA0001", "completed", ConversationDuration="42").json() == {"result": "updated"}
    (job,) = jobs(session_factory)
    (attempt,) = rows(session_factory, CommunicationAttempt)
    (voice,) = rows(session_factory, VoiceSession)
    (followup,) = rows(session_factory, AssignmentFollowup)
    assert (job.status, job.outcome_code) == (CommunicationJobStatus.ACKNOWLEDGED, "DELIVERED_VOICE")
    assert (attempt.status, attempt.outcome_code, attempt.duration_seconds) == (AttemptStatus.DELIVERED, "WILL_SUBMIT", 42)
    assert (voice.status, voice.turn_count, voice.outcome_code) == (VoiceSessionStatus.COMPLETED, 1, "WILL_SUBMIT")
    assert voice.outcome == {"outcome": "WILL_SUBMIT", "turns": 1, "ended_by": "COMPLETED"}
    assert followup.status == FollowupStatus.DELIVERED
    assert status(client, callback_token, "CA0001", "completed").json() == {"result": "ignored"}  # idempotent

    advance(app, orgs["a"], job.agent_mission_id)
    mission, steps = mission_of(session_factory, job.agent_mission_id)
    assert mission.status == AgentMissionStatus.COMPLETED and steps[-1].action_type == "verified_complete"

    # Nothing the student said, and no phone number, was stored anywhere.
    text = json.dumps([{c: str(getattr(v, c)) for c in VoiceSession.__table__.columns.keys()}
                       for v in rows(session_factory, VoiceSession)])
    text += json.dumps([{c: str(getattr(a, c)) for c in CommunicationAttempt.__table__.columns.keys()}
                        for a in rows(session_factory, CommunicationAttempt)])
    text += json.dumps([e.payload for e in rows(session_factory, DomainEvent)])
    text += json.dumps([e.event_metadata for e in rows(session_factory, OperationAuditEvent)])
    text += json.dumps([m.context for m in rows(session_factory, AgentMission)])
    assert "98765" not in text and "99887" not in text and "tonight" not in text


def test_callbacks_need_a_valid_token_for_the_same_call(client, small_class, app, clock, session_factory, orgs) -> None:
    _, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    assert status(client, "forged.token", "CA0001", "completed").status_code == 404
    assert status(client, call["custom_field"], "CA0001", "completed").status_code == 404  # a stream token
    assert status(client, callback_token, "CA9999", "completed").status_code == 404  # another call
    with tenant(session_factory, orgs["b"]) as s:  # the token is bound to organization A's attempt
        assert s.get(CommunicationAttempt, rows(session_factory, CommunicationAttempt)[0].id) is None
    (attempt,) = rows(session_factory, CommunicationAttempt)
    assert attempt.status == AttemptStatus.IN_PROGRESS


def test_no_answer_is_retried_with_backoff(client, small_class, app, clock, session_factory, orgs) -> None:
    _, _, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    assert status(client, callback_token, "CA0001", "no-answer").json() == {"result": "updated"}
    (job,) = jobs(session_factory)
    (attempt,) = rows(session_factory, CommunicationAttempt)
    assert (attempt.status, attempt.error_code) == (AttemptStatus.FAILED, "NO_ANSWER")
    assert (job.status, job.next_attempt_at) == (CommunicationJobStatus.DEFERRED, clock.now + timedelta(minutes=30))
    assert rows(session_factory, VoiceSession)[0].status == VoiceSessionStatus.FAILED
    clock.advance(minutes=30)
    assert [r.outcome for r in comm(app, clock).runs] == ["in_progress"]
    assert len(rows(session_factory, CommunicationAttempt)) == 2


def test_a_missing_final_callback_times_out(client, small_class, app, clock, session_factory, orgs) -> None:
    placed_call(app, client, clock, small_class, session_factory, orgs)
    clock.advance(seconds=180 + 299)
    assert comm(app, clock).runs == []
    clock.advance(seconds=1)
    assert [(r.outcome, r.code) for r in comm(app, clock).runs] == [("call_timed_out", "CALLBACK_TIMEOUT")]
    (job,) = jobs(session_factory)
    assert (job.status, rows(session_factory, CommunicationAttempt)[0].error_code) == (
        CommunicationJobStatus.DEFERRED, "CALLBACK_TIMEOUT")


@pytest.mark.parametrize("kwargs, code", [
    ({"phone": False}, "NO_VERIFIED_CONTACT"),
    ({"verified": False}, "NO_VERIFIED_CONTACT"),
    ({"exotel": FakeExotel("PROVIDER_AUTH_FAILED")}, "PROVIDER_AUTH_FAILED"),
])
def test_non_retryable_voice_failures_fail_the_job(client, small_class, app, clock, session_factory, orgs, kwargs,
                                                   code) -> None:
    voice_setup(app, client, clock, small_class, session_factory, orgs, **kwargs)
    assert [(r.outcome, r.code) for r in comm(app, clock).runs] == [("failed", code)]
    (job,) = jobs(session_factory)
    assert job.status == CommunicationJobStatus.FAILED and rows(session_factory, VoiceSession) == []
    assert rows(session_factory, AssignmentFollowup)[0].status == FollowupStatus.FAILED


def test_rate_limited_provider_is_retried(client, small_class, app, clock, session_factory, orgs) -> None:
    voice_setup(app, client, clock, small_class, session_factory, orgs, exotel=FakeExotel("PROVIDER_RATE_LIMITED"))
    assert [r.outcome for r in comm(app, clock).runs] == ["retry_scheduled"]


# --- The stream and the conversation are bounded -----------------------------------------------------------------------------


def test_stream_is_refused_without_pipeline_or_valid_token(client, small_class, app, clock, session_factory, orgs) -> None:
    _, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    with client.websocket_connect("/communication/voice/stream") as ws:  # no pipeline configured
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1011
    app.state.voice_pipeline = VoicePipeline(stt=FakeSpeech(), tts=FakeSpeech(), brain=ScriptedTurns())
    for token in (callback_token, "forged"):
        with client.websocket_connect("/communication/voice/stream") as ws:
            ws.send_json({"event": "start", "start": {"stream_sid": "ST1", "custom_parameters": {"token": token}}})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008
    assert rows(session_factory, VoiceSession)[0].status == VoiceSessionStatus.PENDING


def _conversation(app, call, brain):
    pipeline = VoicePipeline(stt=FakeSpeech(), tts=FakeSpeech(), brain=brain)
    return VoiceConversation(app.state.session_factory, app.state.agent_runtime.communication, pipeline,
                             call["custom_field"], lambda: app.state.clock())


def test_conversation_ends_at_the_turn_limit_and_on_invalid_turns(client, small_class, app, clock, session_factory,
                                                                   orgs) -> None:
    _, call, _ = placed_call(app, client, clock, small_class, session_factory, orgs)
    conversation = _conversation(app, call, ScriptedTurns())  # the brain never ends the call
    conversation.start("ST1")
    replies = [conversation.on_utterance(b"\x00") for _ in range(4)]
    assert replies[:2] == [b"Sorry, I did not catch that. Could you say it again?"] * 2
    assert replies[2] == b"Thank you. Goodbye." and replies[3] is None and conversation.ended
    (voice,) = rows(session_factory, VoiceSession)
    assert (voice.turn_count, voice.outcome_code) == (3, "OTHER_SAFE")


def test_free_text_turns_are_rejected_safely(client, small_class, app, clock, session_factory, orgs) -> None:
    _, call, _ = placed_call(app, client, clock, small_class, session_factory, orgs)
    conversation = _conversation(app, call, ScriptedTurns({"reply": "Sure! Your marks are 42.", "end_call": False}))
    conversation.start("ST1")
    assert conversation.on_utterance(b"\x00") == b"Thank you. Goodbye." and conversation.ended
    assert rows(session_factory, VoiceSession)[0].outcome == {"outcome": "OTHER_SAFE", "turns": 1, "ended_by": "INVALID_TURN"}


def test_a_request_for_help_is_flagged_for_staff_review(client, small_class, app, clock, session_factory, orgs) -> None:
    job, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    status(client, callback_token, "CA0001", "answered")
    conversation = _conversation(app, call, ScriptedTurns(VoiceTurn(reply="offer_help", outcome=VoiceOutcome.NEEDS_HELP,
                                                                    end_call=True)))
    conversation.start("ST1")
    reply = conversation.on_utterance(bytes(1))
    assert reply == b"I will let your faculty member know that you need help. They will contact you."
    status(client, callback_token, "CA0001", "completed")
    (job,) = jobs(session_factory)
    assert job.status == CommunicationJobStatus.DELIVERED  # help requested: delivered, not acknowledged
    events = [e.event_type for e in rows(session_factory, DomainEvent, DomainEvent.subject_id == str(job.source_mission_id))]
    audits = [e.event_type for e in rows(session_factory, OperationAuditEvent, OperationAuditEvent.subject_id == str(job.id),
                                         OperationAuditEvent.subject_type == "communication_job")]
    assert events.count("COMMUNICATION_RESPONSE_RECEIVED") == 1 and audits.count("COMMUNICATION_RESPONSE_NEEDS_REVIEW") == 1


def test_outcome_applies_once_when_the_call_settles_before_the_stream_ends(client, small_class, app, clock,
                                                                         session_factory, orgs) -> None:
    job, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    status(client, callback_token, "CA0001", "in-progress")
    conversation = _conversation(app, call, ScriptedTurns(VoiceTurn(reply="acknowledge", outcome=VoiceOutcome.ACKNOWLEDGED)))
    conversation.start("ST1")
    conversation.on_utterance(bytes(1))  # outcome recorded, the call continues
    status(client, callback_token, "CA0001", "completed")  # the student hangs up; the provider settles first
    (job,) = jobs(session_factory)
    assert job.status == CommunicationJobStatus.ACKNOWLEDGED
    assert conversation.on_utterance(bytes(1)) is None and conversation.ended  # the stream notices the hang-up
    conversation.stop()
    acknowledged = rows(session_factory, OperationAuditEvent, OperationAuditEvent.event_type == "COMMUNICATION_ACKNOWLEDGED")
    assert len(acknowledged) == 1
    (voice,) = rows(session_factory, VoiceSession)
    assert voice.outcome == {"outcome": "ACKNOWLEDGED", "turns": 1, "ended_by": "CALL_ENDED"}
