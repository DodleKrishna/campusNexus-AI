"""AgentOS V2 Phase 5 / 5.1 (voice): Exotel transport, signed tokens, callbacks, the 16 kHz audio format, WebRTC VAD,
Groq STT/TTS/voice-brain adapters (against a fake HTTP client), the live turn-based pipeline and the WebSocket stream.

Offline: a fake ``ExotelClient``, fake speech providers / VAD / brain, and a fake Groq HTTP client. No provider is
ever called and nothing sleeps.
"""
from __future__ import annotations

import base64
import io
import json
import wave
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.websockets import WebSocketDisconnect

from app.communication.connectors import ConnectorRegistry
from app.communication.sources import SourceFacts
from app.communication.voice import tokens
from app.communication.voice.audio import (
    FRAME_BYTES, AudioFormatError, pcm_to_wav, validate_media_format, wav_to_pcm,
)
from app.communication.voice.brain import GroqVoiceBrain, VoiceBrainInput, VoiceBrainReply, parse_reply
from app.communication.voice.conversation import (
    SAY, CallState, ConversationError, VoiceConversation, VoicePipeline, opening_text, voice_pipeline_from_env,
)
from app.communication.voice.exotel import (
    ExotelConfig, ExotelError, ExotelVoiceConnector, HttpExotelClient, ProviderCall,
)
from app.communication.voice.speech import (
    GroqHTTP, GroqSpeechToText, GroqTextToSpeech, SpeechProviderError, Transcript, VoiceProviderConfig,
)
from app.communication.voice.vad import UtteranceDetector, VadConfig, webrtc_vad
from app.db.models import (
    AgentMission, AgentMissionStatus, AgentStep, AssignmentFollowup, AttemptStatus, CommunicationAttempt,
    CommunicationJob, CommunicationJobStatus, CommunicationPreference, ContactKind, ContactPoint, DomainEvent,
    FollowupStatus, OperationAuditEvent, VoiceSession, VoiceSessionStatus,
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
FORMAT_16K = {"encoding": "base64", "sample_rate": "16000", "bit_rate": "256kbps"}
SPEECH = b"\x01\x02" * (FRAME_BYTES // 2)  # one voiced 20 ms frame (for the fake VAD)
SILENCE = bytes(FRAME_BYTES)
FACTS = SourceFacts("assignment", "Lab 3", "GRD301", T0)


# --- Fakes -------------------------------------------------------------------------------------------------------------


class FakeExotel:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list = []
        self.error = error

    def place_call(self, **kwargs) -> ProviderCall:
        if self.error:
            raise ExotelError(self.error)
        self.calls.append(kwargs)
        return ProviderCall(reference=f"CA{len(self.calls):04d}", status="queued")


class FakeVAD:
    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        assert len(frame) == FRAME_BYTES and sample_rate == 16000
        return frame[0] != 0


class FakeSTT:
    def __init__(self, *results) -> None:
        self.results, self.heard = list(results), []

    def transcribe(self, pcm: bytes) -> Transcript:
        self.heard.append(len(pcm))
        result = self.results.pop(0) if self.results else Transcript(SPOKEN, True)
        if isinstance(result, Exception):
            raise result
        return result


class FakeTTS:
    def __init__(self, fail_on: int | None = None) -> None:
        self.texts, self.fail_on = [], fail_on

    def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        if self.fail_on is not None and len(self.texts) >= self.fail_on:
            raise SpeechProviderError("PROVIDER_UNAVAILABLE", retryable=True)
        data = text.encode("utf-8")
        return data + b" " * (len(data) % 2)  # even length, like 16-bit PCM


class FakeBrain:
    def __init__(self, *replies) -> None:
        self.replies, self.seen = list(replies), []

    def decide(self, context):
        self.seen.append(context)
        reply = self.replies.pop(0) if self.replies else {"reply": "Thanks.", "outcome_code": "ACKNOWLEDGED",
                                                          "continue_conversation": True}
        if isinstance(reply, Exception):
            raise reply
        return reply


def pipeline(stt=None, tts=None, brain=None) -> VoicePipeline:
    return VoicePipeline(stt or FakeSTT(), tts or FakeTTS(), brain or FakeBrain(), vad_factory=FakeVAD)


class FakeResponse:
    def __init__(self, status: int = 200, body=None, content: bytes = b"") -> None:
        self.status_code, self._body, self.content = status, body, content

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHTTP:
    def __init__(self, *responses) -> None:
        self.responses, self.requests = list(responses), []

    def post(self, path, **kwargs):
        self.requests.append((path, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_wav(pcm: bytes, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buffer.getvalue()


# --- Call setup helpers ------------------------------------------------------------------------------------------------


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


def start_frame(token, media_format=FORMAT_16K):
    return json.dumps({"event": "start", "start": {"stream_sid": "ST1", "custom_parameters": {"token": token},
                                                    "media_format": media_format}})


def media(pcm: bytes) -> str:
    return json.dumps({"event": "media", "media": {"payload": base64.b64encode(pcm).decode()}})


def send_audio(ws, pcm: bytes, chunk: int = 1000) -> None:
    """Exotel chunks are not aligned to VAD frames: use an awkward size on purpose."""
    for i in range(0, len(pcm), chunk):
        ws.send_text(media(pcm[i:i + chunk]))


def receive_reply(ws):
    """Collect the assistant's media frames up to (and including) its mark."""
    audio = b""
    while True:
        message = ws.receive_json()
        if message["event"] == "media":
            audio += base64.b64decode(message["media"]["payload"])
        elif message["event"] == "mark":
            return audio.decode("utf-8").strip(), message["mark"]["name"]


def utterance(frames: int = 12) -> bytes:
    return SPEECH * frames + SILENCE * 30


def conversation(app, call, pipe):
    return VoiceConversation(app.state.session_factory, app.state.agent_runtime.communication, pipe,
                             call["custom_field"], lambda: app.state.clock())


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
    url = CONFIG.stream_url("tok.en")
    assert url.startswith("wss://campusnexus.example/communication/voice/stream?") and "sample-rate=16000" in url


def test_live_exotel_client_requests_no_recording(monkeypatch) -> None:
    import httpx

    sent = {}

    def fake_post(url, data, auth, timeout):
        sent.update(data=data, timeout=timeout)
        return FakeResponse(200, {"Call": {"Sid": "CA1", "Status": "queued"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    call = HttpExotelClient(CONFIG).place_call(to=PHONE, caller_id="x", flow_url="f", status_callback="s",
                                               custom_field="c", time_limit_seconds=180)
    assert call.reference == "CA1" and sent["data"]["Record"] == "false" and sent["data"]["TimeLimit"] == "180"
    assert sent["timeout"] == 10.0


def test_stream_url_endpoint_needs_a_stream_token(client, small_class, app, clock, session_factory, orgs) -> None:
    _, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    ok = client.get("/communication/exotel/stream-url", params={"CustomField": call["custom_field"], "From": PHONE})
    assert ok.status_code == 200 and "sample-rate=16000" in ok.json()["url"] and PHONE not in ok.text
    assert client.get("/communication/exotel/stream-url", params={"CustomField": callback_token}).status_code == 404
    assert client.get("/communication/exotel/stream-url", params={"CustomField": "forged"}).status_code == 404


# --- 1-2, 8, 10-11: the audio format ---------------------------------------------------------------------------------


def test_media_format_must_be_16k_linear16() -> None:
    validate_media_format(FORMAT_16K)
    validate_media_format({"encoding": "raw/slin", "sample_rate": 16000})
    for bad, code in (({"encoding": "base64", "sample_rate": "8000"}, "UNSUPPORTED_SAMPLE_RATE"),
                      ({"encoding": "mulaw", "sample_rate": "16000"}, "UNSUPPORTED_ENCODING"),
                      ({"encoding": "base64", "sample_rate": "16000", "bit_rate": "128kbps"}, "UNSUPPORTED_BIT_RATE"),
                      ({"encoding": "base64", "sample_rate": "fast"}, "UNSUPPORTED_SAMPLE_RATE"),
                      (None, "MEDIA_FORMAT_MISSING")):
        with pytest.raises(AudioFormatError, match=code):
            validate_media_format(bad)


def test_pcm_wav_round_trip_and_strict_tts_parsing() -> None:
    pcm = SPEECH * 3
    wav = pcm_to_wav(pcm)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())) == (1, 2, 16000, pcm)
    assert wav_to_pcm(wav, max_bytes=10_000) == pcm  # header stripped: PCM frames only
    for bad, code in ((make_wav(pcm, rate=24000), "TTS_WRONG_SAMPLE_RATE"), (make_wav(pcm, channels=2), "TTS_NOT_MONO"),
                      (make_wav(b"\x01" * 100, width=1), "TTS_NOT_16_BIT"), (b"ID3-not-a-wav", "TTS_NOT_WAV"),
                      (make_wav(b""), "TTS_EMPTY_AUDIO"), (make_wav(pcm), "TTS_AUDIO_TOO_LONG")):
        with pytest.raises(AudioFormatError, match=code):
            wav_to_pcm(bad, max_bytes=1000 if code == "TTS_AUDIO_TOO_LONG" else 10_000)
    with pytest.raises(AudioFormatError):
        pcm_to_wav(b"\x01")


# --- 3-7: VAD ----------------------------------------------------------------------------------------------------------


def detector(**config) -> UtteranceDetector:
    return UtteranceDetector(VadConfig(**config), vad=FakeVAD())


def test_vad_splits_unaligned_chunks_into_20ms_frames() -> None:
    aligned, ragged = detector(), detector()
    stream = utterance()
    out_aligned = aligned.feed(stream)
    out_ragged = [u for i in range(0, len(stream), 999) for u in ragged.feed(stream[i:i + 999])]
    assert out_aligned == out_ragged == [SPEECH * 12]  # trailing silence trimmed, speech start kept
    assert len(ragged._partial) < FRAME_BYTES


def test_vad_silence_and_blips_create_no_utterance() -> None:
    d = detector()
    assert d.feed(SILENCE * 500) == [] and d.buffered_bytes == 0  # 10 s of silence costs nothing
    assert d.feed(SPEECH * 5 + SILENCE * 30) == []  # 100 ms: below the minimum speech duration
    assert d.feed(SPEECH * 2 + SILENCE + SPEECH * 2 + SILENCE * 30) == []  # never 3 voiced frames in a row


def test_vad_bounds_utterance_length_and_memory() -> None:
    d = detector(max_utterance_frames=550)
    peak, out = 0, []
    for _ in range(60 * 50 // 10):  # one minute of continuous speech, 200 ms at a time
        out += d.feed(SPEECH * 10)
        peak = max(peak, d.buffered_bytes)
    assert out and all(len(u) == 550 * FRAME_BYTES for u in out)  # cut at 11 s each
    assert peak <= d.config.max_buffer_bytes


def test_vad_pause_drops_audio_and_reset_is_clean() -> None:
    d = detector()
    d.feed(SPEECH * 8)
    d.pause()
    assert d.feed(utterance()) == [] and d.buffered_bytes == 0  # our own playback / echo: never an utterance
    d.resume()
    assert d.feed(utterance()) == [SPEECH * 12]


def test_vad_config_bounds_and_real_webrtc_vad() -> None:
    for bad in ({"aggressiveness": 4}, {"end_silence_frames": 20}, {"end_silence_frames": 41},
                {"max_utterance_frames": 700}, {"start_frames": 0}):
        with pytest.raises(ValueError):
            VadConfig(**bad)
    vad = webrtc_vad(2)
    assert vad.is_speech(SILENCE, 16000) is False  # real WebRTC VAD accepts exactly one 20 ms 16 kHz frame
    real = UtteranceDetector(VadConfig())
    assert real.feed(SILENCE * 100) == []


# --- 3-4 + 21: Groq adapters (fake HTTP) -----------------------------------------------------------------------------


def test_groq_stt_sends_an_in_memory_wav_and_keeps_text_transient() -> None:
    http = FakeHTTP(FakeResponse(200, {"text": "  I will   submit ", "segments": [{"no_speech_prob": 0.01,
                                                                                    "avg_logprob": -0.2}]}))
    stt = GroqSpeechToText(GroqHTTP("", client=http), "whisper-large-v3-turbo", "en")
    result = stt.transcribe(SPEECH * 4)
    assert result.text == "I will submit" and result.usable and "submit" not in repr(result) and "submit" not in str(result)
    path, kwargs = http.requests[0]
    name, wav, mime = kwargs["files"]["file"]
    assert path == "/audio/transcriptions" and (name, mime) == ("utterance.wav", "audio/wav")
    assert wav_to_pcm(wav, max_bytes=10_000) == SPEECH * 4
    assert kwargs["data"] == {"model": "whisper-large-v3-turbo", "response_format": "verbose_json", "temperature": "0",
                              "language": "en"}


def test_groq_stt_marks_empty_and_low_confidence_unusable() -> None:
    for body in ({"text": ""}, {"text": "uh", "segments": [{"no_speech_prob": 0.9, "avg_logprob": -0.1}]},
                 {"text": "words", "segments": [{"no_speech_prob": 0.1, "avg_logprob": -1.6}]}):
        stt = GroqSpeechToText(GroqHTTP("", client=FakeHTTP(FakeResponse(200, body))))
        assert stt.transcribe(SPEECH).usable is False


def test_groq_provider_policy_is_small_and_bounded() -> None:
    http = FakeHTTP(FakeResponse(503), FakeResponse(200, {"text": "ok"}))
    assert GroqSpeechToText(GroqHTTP("", client=http)).transcribe(SPEECH).text == "ok" and len(http.requests) == 2
    for responses, code in (((FakeResponse(500), FakeResponse(502)), "PROVIDER_UNAVAILABLE"),
                            ((FakeResponse(429),), "PROVIDER_RATE_LIMITED"),
                            ((FakeResponse(401),), "PROVIDER_AUTH_FAILED"),
                            ((TimeoutError("t"), TimeoutError("t")), "PROVIDER_TIMEOUT"),
                            ((FakeResponse(400),), "PROVIDER_REJECTED")):
        http = FakeHTTP(*responses)
        with pytest.raises(SpeechProviderError) as exc:
            GroqSpeechToText(GroqHTTP("", client=http)).transcribe(SPEECH)
        assert exc.value.code == code and len(http.requests) == len(responses) <= 2
        assert SPOKEN not in str(exc.value)


def test_groq_tts_requests_16k_wav_and_returns_pcm_only() -> None:
    http = FakeHTTP(FakeResponse(200, content=make_wav(SPEECH * 2)))
    tts = GroqTextToSpeech(GroqHTTP("", client=http), "canopylabs/orpheus-v1-english", "hannah")
    assert tts.synthesize("Hello there.") == SPEECH * 2
    assert http.requests[0] == ("/audio/speech", {"json": {"model": "canopylabs/orpheus-v1-english", "input": "Hello there.",
                                                           "voice": "hannah", "response_format": "wav",
                                                           "sample_rate": 16000}})
    with pytest.raises(SpeechProviderError, match="TTS_WRONG_SAMPLE_RATE"):
        GroqTextToSpeech(GroqHTTP("", client=FakeHTTP(FakeResponse(200, content=make_wav(SPEECH, rate=24000)))),
                         voice="hannah").synthesize("Hi.")
    with pytest.raises(SpeechProviderError, match="TTS_INPUT_TOO_LONG"):
        tts.synthesize("x" * 201)
    with pytest.raises(ValueError):
        GroqTextToSpeech(GroqHTTP("", client=FakeHTTP()), voice="")


def test_groq_voice_brain_uses_a_strict_schema_without_tools() -> None:
    ok = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
        {"reply": "Thank you, noted.", "outcome_code": "WILL_SUBMIT", "continue_conversation": False})}}]}
    http = FakeHTTP(FakeResponse(200, ok))
    context = VoiceBrainInput(purpose="submission_reminder", kind="assignment", title="Lab 3", course_code="GRD301",
                              when="soon", turn=1, max_turns=3)
    reply = GroqVoiceBrain(GroqHTTP("", client=http), "openai/gpt-oss-20b").decide(context)
    assert reply == VoiceBrainReply(reply="Thank you, noted.", outcome_code=VoiceOutcome.WILL_SUBMIT,
                                    continue_conversation=False)
    body = http.requests[0][1]["json"]
    assert body["response_format"]["json_schema"]["strict"] is True and "tools" not in body
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    for choice in ({"finish_reason": "length", "message": {"content": "{}"}},
                   {"finish_reason": "stop", "message": {"content": None, "tool_calls": [{"id": "x"}]}}):
        with pytest.raises(SpeechProviderError):
            GroqVoiceBrain(GroqHTTP("", client=FakeHTTP(FakeResponse(200, {"choices": [choice]}))), "m").decide(context)


def test_voice_brain_output_is_strictly_validated() -> None:
    good = {"reply": "Thank you.", "outcome_code": "ACKNOWLEDGED", "continue_conversation": True}
    assert parse_reply(good).outcome_code == VoiceOutcome.ACKNOWLEDGED
    assert parse_reply(json.dumps(good)).reply == "Thank you."
    for bad in ({**good, "action": "mark_submitted"}, {**good, "outcome_code": "SUBMITTED"},
                {**good, "reply": "x" * 181}, {**good, "reply": "See https://portal.example.com"},
                {**good, "reply": "Mail hod@college.edu"}, {**good, "reply": "Call 98765 43210"},
                {**good, "continue_conversation": "yes"}, {"reply": "Hi."}, "not json", None):
        with pytest.raises(SpeechProviderError, match="BRAIN_INVALID_OUTPUT"):
            parse_reply(bad)


def test_pipeline_availability_comes_from_the_env(monkeypatch) -> None:
    for name in ("GROQ_API_KEY", "CAMPUSNEXUS_TTS_VOICE", "CAMPUSNEXUS_STT_PROVIDER", "CAMPUSNEXUS_TTS_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    assert voice_pipeline_from_env() == (None, "GROQ_API_KEY_MISSING")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_real")
    assert voice_pipeline_from_env() == (None, "TTS_VOICE_NOT_CONFIGURED")
    monkeypatch.setenv("CAMPUSNEXUS_TTS_PROVIDER", "fake")
    monkeypatch.setenv("CAMPUSNEXUS_TTS_VOICE", "hannah")
    assert voice_pipeline_from_env() == (None, "VOICE_PROVIDER_UNSUPPORTED")  # never silently fake speech
    monkeypatch.setenv("CAMPUSNEXUS_TTS_PROVIDER", "groq")
    live, reason = voice_pipeline_from_env()
    # Phase 6: the providers are wrapped for usage metering (no content recorded).
    assert reason is None and isinstance(live.stt.inner, GroqSpeechToText) and live.tts.inner.voice == "hannah"
    assert (live.stt.provider_name, live.tts.provider_name, live.brain.provider_name) == ("groq", "groq", "groq")
    assert "gsk_test_not_real" not in repr(VoiceProviderConfig.from_env())


# --- 12, 15-17: the pipeline -------------------------------------------------------------------------------------------


def test_opening_is_deterministic_short_and_safe() -> None:
    text = opening_text("submission_reminder", FACTS)
    assert text == opening_text("submission_reminder", FACTS)
    assert text.startswith("Hello. This is CampusNexus calling about your assignment Lab 3 for GRD301, due 07 Oct 2026")
    assert text.endswith("Will you be able to submit it on time?")
    long = SourceFacts("exam", "A" * 120, "GRD301", T0)
    assert len(opening_text("exam_reminder", long)) <= 200 and "A" * 41 not in opening_text("exam_reminder", long)
    assert "your GRD301 class on" in opening_text("absence_check", SourceFacts("class", "GRD301 class", "GRD301", T0))


def test_pipeline_round_trip_and_turn_limit() -> None:
    tts, brain = FakeTTS(), FakeBrain()
    pipe, state = pipeline(tts=tts, brain=brain), CallState("submission_reminder", FACTS, max_turns=3)
    results = [pipe.on_utterance(state, SPEECH) for _ in range(3)]
    assert [r.status for r in results] == ["ANSWERED", "ANSWERED", "MAX_TURNS"]
    assert results[-1].end_call and tts.texts[-1] == "Thanks. Goodbye." and state.turn == 3
    assert brain.seen[1].history[-1].text == SPOKEN and len(brain.seen[2].history) == 5  # transient, in memory
    assert SPOKEN not in repr(state) and "Thanks" not in repr(results[0])


def test_pipeline_failures_are_bounded_and_never_claim_success() -> None:
    stt = FakeSTT(SpeechProviderError("PROVIDER_TIMEOUT", True), SpeechProviderError("PROVIDER_TIMEOUT", True))
    state = CallState("submission_reminder", FACTS, max_turns=3)
    pipe = pipeline(stt=stt)
    first, second = pipe.on_utterance(state, SPEECH), pipe.on_utterance(state, SPEECH)
    assert (first.status, first.end_call, second.status, second.end_call) == ("REPROMPT", False, "STT_FAILED", True)
    assert second.outcome_code == "NO_RESPONSE" and state.turn == 0

    pipe, state = pipeline(stt=FakeSTT(*[Transcript("", False)] * 3)), CallState("submission_reminder", FACTS, 3)
    statuses = [pipe.on_utterance(state, SPEECH).status for _ in range(3)]
    assert statuses == ["REPROMPT", "REPROMPT", "NO_RESPONSE"] and state.turn == 0  # silence never advances a turn

    tts = FakeTTS()
    for bad in (SpeechProviderError("PROVIDER_UNAVAILABLE"), {"reply": "Done! I marked it submitted.",
                                                              "outcome_code": "WILL_SUBMIT", "extra": 1}):
        result = pipeline(tts=tts, brain=FakeBrain(bad)).on_utterance(CallState("x", FACTS, 3), SPEECH)
        assert (result.status, result.end_call, result.outcome_code) == ("BRAIN_FAILED", True, "OTHER_SAFE")
        assert tts.texts[-1] == SAY["brain_failed"]

    result = pipeline(tts=FakeTTS(fail_on=1)).on_utterance(CallState("x", FACTS, 3), SPEECH)
    assert (result.status, result.reply_pcm, result.end_call) == ("TTS_FAILED", None, True)


# --- 1, 12-15, 18-20: the live stream end to end -----------------------------------------------------------------------


def test_live_stream_end_to_end(client, small_class, app, clock, session_factory, orgs) -> None:
    job, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    stt, tts = FakeSTT(), FakeTTS()
    brain = FakeBrain({"reply": "Thank you, I have noted that.", "outcome_code": "WILL_SUBMIT",
                       "continue_conversation": False})
    app.state.voice_pipeline = pipeline(stt, tts, brain)
    assert status(client, callback_token, "CA0001", "in-progress").json() == {"result": "updated"}
    with client.websocket_connect("/communication/voice/stream") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(start_frame(call["custom_field"]))
        opening, mark = receive_reply(ws)
        assert opening == opening_text("submission_reminder", SourceFacts("assignment", "Lab 3", "GRD301",
                                                                          deadline_of(session_factory)))
        assert mark == "cn-1"
        send_audio(ws, utterance())  # spoken during our opening (or its echo): never transcribed
        assert stt.heard == []
        ws.send_text(json.dumps({"event": "mark", "mark": {"name": "cn-1"}}))  # playback finished
        send_audio(ws, utterance())
        reply, mark = receive_reply(ws)
        assert (reply, mark, stt.heard) == ("Thank you, I have noted that.", "cn-2", [12 * FRAME_BYTES])
        ws.send_text(json.dumps({"event": "mark", "mark": {"name": "cn-2"}}))  # the final reply has played
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    context = brain.seen[0]
    assert (context.turn, context.title, context.history[-1].text) == (1, "Lab 3", SPOKEN)
    (voice,) = rows(session_factory, VoiceSession)
    assert (voice.status, voice.turn_count, voice.outcome_code) == (VoiceSessionStatus.CONNECTED, 1, "WILL_SUBMIT")
    assert voice.outcome["ended_by"] == "COMPLETED" and voice.outcome["turns"] == 1

    assert status(client, callback_token, "CA0001", "completed", ConversationDuration="42").json() == {"result": "updated"}
    (job,) = jobs(session_factory)
    (voice,) = rows(session_factory, VoiceSession)
    assert (job.status, voice.status) == (CommunicationJobStatus.ACKNOWLEDGED, VoiceSessionStatus.COMPLETED)
    assert rows(session_factory, AssignmentFollowup)[0].status == FollowupStatus.DELIVERED
    advance(app, orgs["a"], job.agent_mission_id)
    mission, steps = mission_of(session_factory, job.agent_mission_id)
    assert mission.status == AgentMissionStatus.COMPLETED and steps[-1].action_type == "verified_complete"
    assert_nothing_sensitive_stored(session_factory)


def deadline_of(session_factory):
    from app.db.models import Assignment

    return rows(session_factory, Assignment)[0].deadline_at


def assert_nothing_sensitive_stored(session_factory) -> None:
    def dump(model):
        return json.dumps([{c: str(getattr(r, c)) for c in model.__table__.columns.keys()}
                           for r in rows(session_factory, model)])

    text = "".join(dump(m) for m in (VoiceSession, CommunicationAttempt, CommunicationJob, AgentStep))
    text += json.dumps([e.payload for e in rows(session_factory, DomainEvent)])
    text += json.dumps([e.event_metadata for e in rows(session_factory, OperationAuditEvent)])
    text += json.dumps([m.context for m in rows(session_factory, AgentMission)])
    for secret in ("98765", "99887", "tonight", "yes I will", "meridian", "noted that", "Hello. This is"):
        assert secret not in text
    for model in (VoiceSession, CommunicationAttempt):
        assert not {"transcript", "audio", "text", "phone", "email"} & set(model.__table__.columns.keys())


def test_stream_rejects_wrong_format_bad_tokens_and_duplicates(client, small_class, app, clock, session_factory,
                                                               orgs) -> None:
    _, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    app.state.voice_pipeline = None
    with client.websocket_connect("/communication/voice/stream") as ws:  # pipeline unavailable: refused
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1011
    app.state.voice_pipeline = pipeline()
    for frame, code in ((start_frame(call["custom_field"], {"encoding": "base64", "sample_rate": "8000"}), 1003),
                        (start_frame(callback_token), 1008), (start_frame("forged.token"), 1008)):
        with client.websocket_connect("/communication/voice/stream") as ws:
            ws.send_text(frame)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == code
    assert rows(session_factory, VoiceSession)[0].status == VoiceSessionStatus.PENDING  # nothing was claimed

    with client.websocket_connect("/communication/voice/stream") as first:
        first.send_text(start_frame(call["custom_field"]))
        receive_reply(first)
        with client.websocket_connect("/communication/voice/stream") as replay:  # the same token again
            replay.send_text(start_frame(call["custom_field"]))
            with pytest.raises(WebSocketDisconnect) as exc:
                replay.receive_json()
            assert exc.value.code == 1008
    clock.advance(hours=3)  # past the token's expiry
    with client.websocket_connect("/communication/voice/stream") as ws:
        ws.send_text(start_frame(call["custom_field"]))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008


def test_stream_bounds_frames_media_and_call_audio(client, small_class, app, clock, session_factory, orgs) -> None:
    _, call, _ = placed_call(app, client, clock, small_class, session_factory, orgs)
    app.state.voice_pipeline = pipeline()
    with client.websocket_connect("/communication/voice/stream") as ws:
        ws.send_text(start_frame(call["custom_field"]))
        receive_reply(ws)
        ws.send_text(json.dumps({"event": "media", "media": {"payload": "A" * 40_000}}))  # > one JSON frame
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1009


def test_cumulative_call_audio_is_bounded(client, small_class, app, clock, session_factory, orgs) -> None:
    _, call, _ = placed_call(app, client, clock, small_class, session_factory, orgs)
    app.state.voice_pipeline = pipeline()
    with client.websocket_connect("/communication/voice/stream") as ws:
        ws.send_text(start_frame(call["custom_field"]))
        receive_reply(ws)
        chunk = SILENCE * 25  # 0.5 s
        with pytest.raises(WebSocketDisconnect):
            for _ in range((180 + 30) * 2 + 2):
                ws.send_text(media(chunk))
            ws.receive_json()
    (voice,) = rows(session_factory, VoiceSession)
    assert voice.outcome["ended_by"] == "STREAM_STOPPED" and voice.outcome_code == "NO_RESPONSE"


def test_playback_resumes_without_a_mark_only_after_it_has_played(client, small_class, app, clock, session_factory,
                                                                   orgs) -> None:
    _, call, _ = placed_call(app, client, clock, small_class, session_factory, orgs)
    stt = FakeSTT()
    app.state.voice_pipeline = pipeline(stt=stt, brain=FakeBrain({"reply": "Okay.", "outcome_code": "ACKNOWLEDGED",
                                                                  "continue_conversation": False}))
    with client.websocket_connect("/communication/voice/stream") as ws:
        ws.send_text(start_frame(call["custom_field"]))
        receive_reply(ws)
        send_audio(ws, utterance())
        assert stt.heard == []
        clock.advance(seconds=5)  # the opening (and the grace) has certainly played; Exotel lost the mark
        send_audio(ws, utterance())
        assert receive_reply(ws)[0] == "Okay." and len(stt.heard) == 1


# --- Failures through the database ------------------------------------------------------------------------------------


def test_tts_failure_fails_the_attempt_for_a_bounded_retry(client, small_class, app, clock, session_factory, orgs) -> None:
    _, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    with pytest.raises(ConversationError, match="TTS_FAILED"):
        conversation(app, call, pipeline(tts=FakeTTS(fail_on=1))).start("ST1")
    (job,) = jobs(session_factory)
    (attempt,) = rows(session_factory, CommunicationAttempt)
    (voice,) = rows(session_factory, VoiceSession)
    assert (attempt.status, attempt.error_code) == (AttemptStatus.FAILED, "VOICE_PIPELINE_FAILED")
    assert (job.status, job.next_attempt_at) == (CommunicationJobStatus.DEFERRED, clock.now + timedelta(minutes=30))
    assert voice.status == VoiceSessionStatus.FAILED
    assert status(client, callback_token, "CA0001", "completed").json() == {"result": "ignored"}  # already final
    clock.advance(minutes=30)
    comm(app, clock)
    attempt2 = rows(session_factory, CommunicationAttempt)[-1]
    with pytest.raises(ConversationError):
        conversation(app, {"custom_field": tokens.sign("stream", orgs["a"], attempt2.id, clock.now + timedelta(hours=1))},
                     pipeline(tts=FakeTTS(fail_on=1))).start("ST2")
    (job,) = jobs(session_factory)
    assert (job.status, job.attempt_count) == (CommunicationJobStatus.FAILED, 2)  # no infinite retries


def test_a_request_for_help_is_flagged_for_staff_review(client, small_class, app, clock, session_factory, orgs) -> None:
    job, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    status(client, callback_token, "CA0001", "answered")
    conv = conversation(app, call, pipeline(brain=FakeBrain({"reply": "I will let your faculty member know.",
                                                             "outcome_code": "NEEDS_HELP",
                                                             "continue_conversation": False})))
    conv.start("ST1")
    assert conv.on_utterance(SPEECH).status == "COMPLETED"
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
    conv = conversation(app, call, pipeline())  # the default fake brain acknowledges and continues
    conv.start("ST1")
    conv.on_utterance(SPEECH)
    status(client, callback_token, "CA0001", "completed")  # the student hangs up; the provider settles first
    (job,) = jobs(session_factory)
    assert job.status == CommunicationJobStatus.ACKNOWLEDGED
    assert conv.on_utterance(SPEECH).status == "ENDED" and conv.ended
    conv.stop()
    assert len(rows(session_factory, OperationAuditEvent, OperationAuditEvent.event_type == "COMMUNICATION_ACKNOWLEDGED")) == 1
    (voice,) = rows(session_factory, VoiceSession)
    assert (voice.outcome["outcome"], voice.outcome["turns"], voice.outcome["ended_by"]) == ("ACKNOWLEDGED", 1, "CALL_ENDED")


# --- Provider-side failures (transport) ---------------------------------------------------------------------------------


def test_callbacks_need_a_valid_token_for_the_same_call(client, small_class, app, clock, session_factory, orgs) -> None:
    _, call, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    assert status(client, "forged.token", "CA0001", "completed").status_code == 404
    assert status(client, call["custom_field"], "CA0001", "completed").status_code == 404  # a stream token
    assert status(client, callback_token, "CA9999", "completed").status_code == 404  # another call
    with tenant(session_factory, orgs["b"]) as s:  # the token is bound to organization A's attempt
        assert s.get(CommunicationAttempt, rows(session_factory, CommunicationAttempt)[0].id) is None
    assert rows(session_factory, CommunicationAttempt)[0].status == AttemptStatus.IN_PROGRESS


def test_no_answer_is_retried_with_backoff(client, small_class, app, clock, session_factory, orgs) -> None:
    _, _, callback_token = placed_call(app, client, clock, small_class, session_factory, orgs)
    assert status(client, callback_token, "CA0001", "no-answer").json() == {"result": "updated"}
    (job,) = jobs(session_factory)
    (attempt,) = rows(session_factory, CommunicationAttempt)
    assert (attempt.status, attempt.error_code) == (AttemptStatus.FAILED, "NO_ANSWER")
    assert (job.status, job.next_attempt_at) == (CommunicationJobStatus.DEFERRED, clock.now + timedelta(minutes=30))
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
