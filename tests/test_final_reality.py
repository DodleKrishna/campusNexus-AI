"""Final end-to-end reality checks: Nexus ingress, offline operation, one voice path, autonomous exam reminders and
the absent-student call path -- on the seeded CAMPUS AI demo campus (``tests/test_grounded_campus_ai.campus``).

No live model, no network, no real phone call: brains and providers are in-process fakes that count their calls, and
Exotel is a fake client. What is proved here is the wiring and the guarantees (no model for greetings/records, zero
cloud calls offline, the same router for voice, autonomous wake-ups, no duplicate or post-resolution contact, no
phone number or transcript persisted). A physical call and a physical microphone still need manual qualification.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agentos.brain import BrainUnavailableError
from app.agentos.brain_router import ConnectivityAwareBrainRouter
from app.agentos.connectivity import ConnectivityService, ConnectivityState
from app.agentos.ingress import GREETING_REPLY, IngressIntent, classify_ingress
from app.agentos.schemas import AgentDecision, DecisionKind
from app.communication.voice.speech import Transcript
from app.db.models.academic import Exam
from app.db.models.agent_kernel import AgentMission, AgentStep, DomainEvent
from app.db.models.communication import Notification
from app.db.models.communication_delivery import CommunicationJob, CommunicationJobStatus
from app.db.models.identity import Student
from app.db.models.workflow import OperationAuditEvent
from app.db.session import create_session_factory
from app.schemas.enums import UserRole
from app.services.grounded_answers import classify as grounded_classify
from tests.test_grounded_campus_ai import NOW, PASSWORD, campus  # noqa: F401  (module-scoped seeded campus)

TEST_PHONE = "+910000012345"  # a test-only placeholder, never a real number
DEMO_STUDENT = "STU-DEMO-001"


# --- Fakes ---------------------------------------------------------------------------------------------------------


class CountingBrain:
    """A brain that records every decision it is asked for (and can be unavailable)."""

    audit_calls = False

    def __init__(self, name: str, *, available: bool = True, reply: str = "general reply") -> None:
        self.provider_name, self.model_name, self.is_live = name, f"{name}-model", True
        self.available, self.code, self.reply, self.calls = available, f"{name.upper()}_DOWN", reply, 0

    def decide(self, context):
        self.calls += 1
        if not self.available:
            raise BrainUnavailableError("down", code=self.code)
        return AgentDecision(kind=DecisionKind.COMPLETE, outcome="answered", user_message=self.reply)


class CountingProvider:
    """A 'live' synthesis provider that must never be called offline."""

    name, model_name, is_live = "openrouter", "fake", True

    def __init__(self) -> None:
        self.calls = 0

    def synthesize_grounded_answer(self, prompt):
        self.calls += 1
        raise AssertionError("no synthesis call is allowed here")


class FakeSpeech:
    """The configured STT/TTS (whisper.cpp / Kokoro in an edge deployment)."""

    def __init__(self, text: str) -> None:
        self.text, self.spoken = text, []

    def transcribe(self, pcm):
        return Transcript(self.text, True), "whisper_cpp"

    def synthesize(self, text):
        self.spoken.append(text)
        return b"\x05\x00" * 1600, "kokoro"

    def status(self):
        return {"stt_provider": "whisper_cpp", "tts_provider": "kokoro", "local_ready": True, "unavailable": None}


def _wav(seconds: float = 1.0) -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * int(16000 * seconds))
    return buffer.getvalue()


def _app(campus, brain, *, connectivity=None, speech=None):  # noqa: F811
    from app.api.main import create_app
    from app.llm.providers.mock import MockLLMProvider
    from app.services.knowledge import KnowledgeService

    class NoPolicies(KnowledgeService):
        def __init__(self):
            pass

        def indexed_chunk_count(self):
            return 0

    return create_app(session_factory=create_session_factory(campus["engine"]), knowledge_service=NoPolicies(),
                      llm_provider=MockLLMProvider(), clock=lambda: NOW, campus_knowledge=campus["knowledge"],
                      agent_brain=brain, connectivity=connectivity, speech_router=speech)


def _token(client, email: str) -> dict:
    token = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _say(client, headers, message: str):
    return client.post("/agentos/assistant/message", json={"message": message}, headers=headers)


# --- Part 1: ingress classification ----------------------------------------------------------------------------------


@pytest.mark.parametrize("message", ["hi", "Hello", "hey nexus", "Good morning!", "hello nexus", "hi there",
                                     "thanks", "good evening, Nexus"])
def test_greetings_are_recognised(message) -> None:
    assert classify_ingress(message, UserRole.STUDENT, grounded_classify) == IngressIntent.GREETING


@pytest.mark.parametrize("message,intent", [
    ("When is my next exam?", IngressIntent.GROUNDED_CAMPUS_QUERY),
    ("What is the hostel complaint policy?", IngressIntent.GROUNDED_CAMPUS_QUERY),
    ("hi, when is my next exam?", IngressIntent.GROUNDED_CAMPUS_QUERY),
    ("Help me plan my semester", IngressIntent.MISSION_REQUEST),
    ("Remind me to study tonight", IngressIntent.MISSION_REQUEST),
    ("Register me for the hackathon", IngressIntent.UNSUPPORTED_ACTION),
    ("Book me a hostel room", IngressIntent.UNSUPPORTED_ACTION),
    ("Tell me a fun fact about space", IngressIntent.GENERAL_CONVERSATION),
    ("Who am I?", IngressIntent.GENERAL_CONVERSATION),
])
def test_ingress_routes_each_kind_of_message(message, intent) -> None:
    assert classify_ingress(message, UserRole.STUDENT, grounded_classify) == intent


def test_without_grounded_answers_nothing_is_a_records_query() -> None:
    assert classify_ingress("When is my next exam?", UserRole.STUDENT, None) == IngressIntent.GENERAL_CONVERSATION


# --- Part 1 + 5: greetings, records and conversation through the API --------------------------------------------------


def test_greeting_needs_no_model_even_with_every_brain_down(campus) -> None:  # noqa: F811
    cloud, local = CountingBrain("openrouter", available=False), CountingBrain("ollama", available=False)
    brain = ConnectivityAwareBrainRouter("auto", connectivity=ConnectivityService(forced=ConnectivityState.LOCAL_ONLY),
                                         cloud=cloud, local=local)
    with TestClient(_app(campus, brain)) as client:
        headers = _token(client, "student@campusnexus.local")
        for message in ("hi", "hello nexus", "Good morning"):
            body = _say(client, headers, message)
            assert body.status_code == 200, body.text
            reply = body.json()
            assert reply["assistant_message"] == GREETING_REPLY and reply["route"] == "greeting"
            assert reply["mission_id"] is None and reply["brain"] is None
        faculty = _say(client, _token(client, "faculty@campusnexus.local"), "hey nexus").json()
        assert "attendance sessions" in faculty["assistant_message"]
    assert cloud.calls == 0 and local.calls == 0


def test_campus_query_uses_the_grounded_pipeline_and_conversation_uses_the_brain(campus) -> None:  # noqa: F811
    cloud = CountingBrain("openrouter", reply="Space is big.")
    brain = ConnectivityAwareBrainRouter("cloud", connectivity=ConnectivityService(forced=ConnectivityState.CLOUD_REACHABLE),
                                         cloud=cloud)
    with TestClient(_app(campus, brain)) as client:
        headers = _token(client, "student@campusnexus.local")
        exam = _say(client, headers, "When is my next exam?").json()
        assert exam["route"] == "grounded_campus_query" and "Quiz 2: Transport Layer" in exam["assistant_message"]
        assert cloud.calls == 0  # the grounded router decided both transitions; no AI decision
        chat = _say(client, headers, "Tell me a fun fact about space").json()
        assert chat["route"] == "general_conversation" and chat["assistant_message"] == "Space is big."
        assert cloud.calls == 1
        plan = _say(client, headers, "Help me plan my semester").json()
        assert plan["route"] == "mission_request" and plan["mission_id"] is not None and cloud.calls == 2


def test_an_action_request_with_no_brain_gets_the_workflow_pointer(campus) -> None:  # noqa: F811
    brain = ConnectivityAwareBrainRouter("local", connectivity=ConnectivityService(forced=ConnectivityState.LOCAL_ONLY),
                                         local=CountingBrain("ollama", available=False))
    with TestClient(_app(campus, brain)) as client:
        headers = _token(client, "student@campusnexus.local")
        body = _say(client, headers, "Register me for the hackathon").json()
        assert body["route"] == "unsupported_action" and "approval workflow" in body["assistant_message"]
        assert body["mission_id"] is None
        refused = _say(client, headers, "Tell me a fun fact about space")
        assert refused.status_code == 503  # genuine reasoning needs a brain: never a fake answer


def test_offline_greeting_records_and_policy_answers_make_zero_model_calls(campus, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv("CAMPUSNEXUS_OFFLINE_MODE", "1")
    monkeypatch.setenv("CAMPUSNEXUS_INTELLIGENCE_MODE", "auto")
    cloud, local = CountingBrain("openrouter"), CountingBrain("ollama", available=False)  # Ollama not even running
    net = ConnectivityService(forced=ConnectivityState.LOCAL_ONLY)
    brain = ConnectivityAwareBrainRouter("auto", connectivity=net, cloud=cloud, local=local)
    app = _app(campus, brain, connectivity=net)
    synthesis = CountingProvider()
    app.state.grounded_answers.provider = synthesis  # a live cloud provider is configured, but must stay unused
    with TestClient(app) as client:
        headers = _token(client, "student@campusnexus.local")
        assert _say(client, headers, "hi").json()["assistant_message"] == GREETING_REPLY
        exam = _say(client, headers, "When is my next exam?").json()
        assert "Quiz 2: Transport Layer" in exam["assistant_message"]
        policy = _say(client, headers, "What is the hostel complaint policy?").json()
        assert "[CAI-SOP-HOS-002]" in policy["assistant_message"]
        health = client.get("/health").json()
        assert health["connectivity"] == "offline" and health["intelligence"]["active_provider"] is None  # Ollama is down too
    assert (cloud.calls, local.calls, synthesis.calls) == (0, 0, 0)


# --- Part 2: voice uses the same router ------------------------------------------------------------------------------


def test_voice_goes_through_the_same_router_and_keeps_no_transcript(campus) -> None:  # noqa: F811
    spoken = "When is my next exam?"
    speech = FakeSpeech(spoken)
    cloud = CountingBrain("openrouter")
    brain = ConnectivityAwareBrainRouter("cloud", connectivity=ConnectivityService(forced=ConnectivityState.CLOUD_REACHABLE),
                                         cloud=cloud)
    with TestClient(_app(campus, brain, speech=speech)) as client:
        headers = {**_token(client, "student@campusnexus.local"), "Content-Type": "audio/wav"}
        reply = client.post("/agentos/assistant/voice", content=_wav(), headers=headers)
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["route"] == "grounded_campus_query" and "Quiz 2: Transport Layer" in body["assistant_message"]
        assert body["speech"] == {"stt_provider": "whisper_cpp", "tts_provider": "kokoro"}
        assert base64.b64decode(body["audio_wav_base64"])[:4] == b"RIFF" and speech.spoken
        assert "transcript" not in body and cloud.calls == 0
        speech.text = "hello nexus"
        greeting = client.post("/agentos/assistant/voice", content=_wav(), headers=headers)
        assert greeting.json()["assistant_message"] == GREETING_REPLY and greeting.json()["mission_id"] is None
    with campus["factory"].open_tenant_session(campus["org"]) as session:
        mission = session.get(AgentMission, body["mission_id"])
        assert mission.goal != spoken and spoken not in str(mission.context)
        steps = session.execute(select(AgentStep).where(AgentStep.mission_id == mission.id)).scalars().all()
        assert all(spoken not in str(s.input_summary) for s in steps)


# --- Part 4: autonomous exam reminder ----------------------------------------------------------------------------------


def _runtime(clock, **kwargs):
    from app.agentos.bootstrap import build_agent_runtime
    from app.agentos.providers import MockAgentBrain

    return build_agent_runtime(MockAgentBrain(), clock=clock, **kwargs)


def _tick(campus, runtime, at, rounds: int = 3):  # noqa: F811
    """What the loop worker does per batch, a few batches at the same instant."""
    from app.agentos.attendance_guardian import scan_attendance
    from app.agentos.worker import process_due_missions
    from app.communication.worker import process_communication_jobs

    for _ in range(rounds):
        scan_attendance(campus["factory"], runtime, now=at)
        process_communication_jobs(campus["factory"], runtime, now=at)
        process_due_missions(campus["factory"], runtime, now=at, limit=50)


def _notifications(campus, needle: str):  # noqa: F811
    with campus["factory"].open_tenant_session(campus["org"]) as session:
        student = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
        return [n for n in session.execute(select(Notification).where(Notification.student_id == student.id)).scalars()
                if needle in (n.body or "")]


def test_exam_guardian_wakes_at_its_checkpoint_and_reminds_without_user_action(campus) -> None:  # noqa: F811
    from scripts.demo_scenarios import arm_exam_reminder

    armed = arm_exam_reminder(campus["engine"], in_minutes=3, now=NOW)
    checkpoint = datetime.fromisoformat(armed["reminder_checkpoint_at"])
    assert timedelta(minutes=3) <= checkpoint - NOW <= timedelta(minutes=5)
    with campus["factory"].open_tenant_session(campus["org"]) as session:
        exam_title = session.get(AgentMission, armed["guardian_mission_id"]) and \
            session.execute(select(__import__("app.db.models.academic", fromlist=["Exam"]).Exam.title).where(
                __import__("app.db.models.academic", fromlist=["Exam"]).Exam.id == armed["exam_id"])).scalar_one()

    clock = [NOW]
    runtime = _runtime(lambda: clock[0])
    _tick(campus, runtime, NOW)
    with campus["factory"].open_tenant_session(campus["org"]) as session:
        guardian = session.get(AgentMission, armed["guardian_mission_id"])
        assert guardian.status.value.startswith("waiting") and guardian.next_wake_at == checkpoint
    assert _notifications(campus, exam_title) == []

    clock[0] = NOW + timedelta(minutes=2)  # before the checkpoint: still asleep
    _tick(campus, runtime, clock[0])
    assert _notifications(campus, exam_title) == []

    clock[0] = checkpoint + timedelta(seconds=5)  # the checkpoint came due: no user action anywhere
    _tick(campus, runtime, clock[0], rounds=4)
    delivered = _notifications(campus, exam_title)
    assert len(delivered) == 1 and delivered[0].title.startswith("Upcoming exam")
    with campus["factory"].open_tenant_session(campus["org"]) as session:
        guardian = session.get(AgentMission, armed["guardian_mission_id"])
        assert guardian.status.value.startswith("waiting") and guardian.next_wake_at > clock[0]  # next checkpoint
        observations = guardian.context["observations"]
        assert any(o["kind"] == "wake" for o in observations)

    restarted = _runtime(lambda: clock[0])  # a crashed/restarted worker never repeats the reminder
    _tick(campus, restarted, clock[0] + timedelta(seconds=30), rounds=3)
    assert len(_notifications(campus, exam_title)) == 1


# --- Part 3: absent student -> Attendance Guardian -> Communication -> (fake) Exotel call ------------------------------


class FakeExotel:
    def __init__(self) -> None:
        self.calls = []

    def place_call(self, **kwargs):
        from app.communication.voice.exotel import ProviderCall

        self.calls.append(kwargs)
        return ProviderCall(reference=f"CA-demo-{len(self.calls)}", status="queued")


def test_absent_demo_student_is_called_through_the_guardian_and_nothing_leaks(campus, monkeypatch) -> None:  # noqa: F811
    from app.communication.connectors import ConnectorRegistry
    from app.communication.service import policy_from_env
    from app.communication.voice.callbacks import StatusCallback, handle_status_callback
    from app.communication.voice.exotel import ExotelConfig, ExotelVoiceConnector
    from app.rules.attendance_intervention import AttendancePolicy
    from app.rules.followup_policy import FollowupPolicy
    from scripts.demo_scenarios import arm_absence, set_voice_preference

    monkeypatch.setenv("CAMPUSNEXUS_DEMO_MODE", "1")
    monkeypatch.setenv("CAMPUSNEXUS_TEST_PHONE", TEST_PHONE)
    policy = policy_from_env()
    assert policy.voice_time_limit_seconds == 60  # demo calls are capped at one minute
    config = ExotelConfig(account_sid="acct", api_key="k", api_token="t", caller_id="0800",
                          public_base_url="https://demo.example")
    fake = FakeExotel()
    connector = ExotelVoiceConnector(config, client=fake, policy=policy)
    clock = [NOW]
    runtime = _runtime(lambda: clock[0], communication_policy=policy, connectors=ConnectorRegistry([connector]),
                       attendance_policy=AttendancePolicy(contact=FollowupPolicy(), grace=timedelta(minutes=1)))

    set_voice_preference(campus["engine"], enabled=True)
    armed = arm_absence(campus["engine"], start_and_mark=True, now=NOW)
    assert armed["started"] and armed["absent_student"] == DEMO_STUDENT

    clock[0] = NOW + timedelta(minutes=2)  # after the (demo) grace period
    _tick(campus, runtime, clock[0], rounds=4)
    assert len(fake.calls) == 1  # one call, to the designated demo student only
    call = fake.calls[0]
    assert call["to"] == TEST_PHONE and call["time_limit_seconds"] == 60
    assert call["stream_url"].startswith("wss://demo.example/communication/voice/stream?sample-rate=16000&token=")
    assert _notifications(campus, "recorded absent")  # the in-app notice went out at once, alongside the call
    with campus["factory"].open_tenant_session(campus["org"]) as session:
        job = session.execute(select(CommunicationJob).where(CommunicationJob.selected_channel == "voice")).scalars().one()
        assert job.status == CommunicationJobStatus.IN_PROGRESS  # Exotel accepting the request is not a delivery

    token = parse_qs(urlparse(call["status_callback"]).query)["token"][0]
    for status in ("in-progress", "completed"):
        handle_status_callback(campus["factory"], runtime.communication,
                               StatusCallback(token=token, call_sid="CA-demo-1", status=status, duration_seconds=40),
                               clock[0])
    _tick(campus, runtime, clock[0] + timedelta(seconds=10), rounds=2)
    with campus["factory"].open_tenant_session(campus["org"]) as session:
        job = session.get(CommunicationJob, job.id)
        assert job.status == CommunicationJobStatus.DELIVERED
        events = [e.event_type for e in session.execute(select(DomainEvent)).scalars()]
        assert "COMMUNICATION_DELIVERED" in events
        audit_text = " ".join(str(a.event_metadata) for a in session.execute(select(OperationAuditEvent)).scalars())
        step_text = " ".join(str(s.input_summary) + str(s.output_summary)
                             for s in session.execute(select(AgentStep)).scalars())
        event_text = " ".join(str(e.payload) for e in session.execute(select(DomainEvent)).scalars())
    digits = TEST_PHONE.lstrip("+")
    assert digits not in audit_text and digits not in step_text and digits not in event_text
    campus["engine"].dispose()
    assert digits.encode() not in (campus["root"] / "campus.db").read_bytes()  # never persisted anywhere

    _tick(campus, runtime, clock[0] + timedelta(hours=7), rounds=2)  # later batches: no further call, it is resolved
    assert len(fake.calls) == 1
    set_voice_preference(campus["engine"], enabled=False)


def test_without_demo_mode_the_test_phone_is_never_used(campus, monkeypatch) -> None:  # noqa: F811
    from app.communication.contacts import ContactResolver, demo_call_student_code
    from app.communication.voice.exotel import ExotelConfig, ExotelVoiceConnector
    from app.db.models.communication_delivery import ContactKind

    monkeypatch.setenv("CAMPUSNEXUS_TEST_PHONE", TEST_PHONE)
    monkeypatch.delenv("CAMPUSNEXUS_DEMO_MODE", raising=False)
    assert demo_call_student_code() is None
    monkeypatch.setenv("CAMPUSNEXUS_DEMO_MODE", "1")
    connector = ExotelVoiceConnector(ExotelConfig(), client=FakeExotel())
    with campus["factory"].open_tenant_session(campus["org"]) as session:
        students = {s.student_code: s.id for s in session.execute(select(Student)).scalars()}
        other = next(code for code in students if code != DEMO_STUDENT)
        assert ContactResolver().resolve(session, connector, students[other], ContactKind.PHONE) is None
        demo = ContactResolver().resolve(session, connector, students[DEMO_STUDENT], ContactKind.PHONE)
        assert demo is not None and demo.verified and TEST_PHONE not in repr(demo)


def test_health_reports_the_call_transport_honestly(campus, monkeypatch) -> None:  # noqa: F811
    for name in ("EXOTEL_API_KEY", "EXOTEL_API_TOKEN", "EXOTEL_ACCOUNT_SID", "EXOTEL_CALLER_ID"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(_app(campus, CountingBrain("openrouter"))) as client:
        comm = client.get("/health").json()["communication"]
    assert comm["voice_call"] == "unavailable" and comm["voice_call_reason"] == "EXOTEL_NOT_CONFIGURED"
    assert comm["in_app"] == "available"


def test_short_absence_grace_is_demo_only(monkeypatch) -> None:
    from app.agentos.attendance_guardian import policy_from_env

    monkeypatch.setenv("CAMPUSNEXUS_ATTENDANCE_ABSENCE_GRACE_MINUTES", "1")
    monkeypatch.delenv("CAMPUSNEXUS_DEMO_MODE", raising=False)
    with pytest.raises(ValueError):
        policy_from_env()
    monkeypatch.setenv("CAMPUSNEXUS_DEMO_MODE", "1")
    assert policy_from_env().grace == timedelta(minutes=1)


# --- Worker loop -----------------------------------------------------------------------------------------------------


def test_worker_loop_runs_bounded_batches_and_stops_cleanly() -> None:
    from scripts.process_due_missions import StopFlag, run_loop

    stop, ran = StopFlag(), []

    def batch():
        ran.append(1)
        if len(ran) == 2:
            raise RuntimeError("database blip")  # reported, retried next tick
        if len(ran) == 3:
            stop.set()  # Ctrl+C during a batch: that batch finishes, then the loop exits
        return {"ok": True}

    assert run_loop(batch, interval=0.01, stop=stop) == 0 and len(ran) == 3
    ran.clear()
    assert run_loop(batch, interval=0.01, stop=StopFlag(), once=True) == 0 and len(ran) == 1
