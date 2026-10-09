"""Exotel voice provider (AgentOS V2 Phase 5): configuration, the call-placing client and the voice connector.

``ExotelVoiceConnector`` is a ``DeliveryConnector``: it is the only place a phone number is read (through
``ContactResolver``, verified numbers only), and the number goes straight into the provider call -- it is never
stored, logged, audited or returned. One delivery attempt = one outbound call.

Call modes (``EXOTEL_CALL_MODE``):

* ``stream`` (default) -- Exotel's Connect Voice AI API: ``POST https://{subdomain}/v1/Accounts/{sid}/Calls/connect``
  with ``From`` (the student), ``CallerId`` (the ExoPhone), ``StreamUrl`` (our public ``wss`` stream at 16 kHz, carrying
  the call's signed stream token) and ``StreamType=bidirectional``. No Exotel flow is needed.
* ``flow`` -- the Phase 5 path: the call connects to an Exotel flow (``EXOTEL_FLOW_APP_ID``) whose Voicebot applet takes
  its stream URL from ``GET /communication/exotel/stream-url`` (the stream token travels as ``CustomField``).

Both send ``Record=false``, ``TimeLimit`` (the policy's voice time limit) and ``StatusCallback`` with the ``answered``
and ``terminal`` events. The call is asynchronous: ``deliver`` returns ``in_progress`` -- Exotel accepting the request
is NOT a delivery -- and the provider's status callback (``callbacks.handle_status_callback``) settles the attempt.
``verify`` confirms a delivery only from that recorded outcome (answered and completed).

Configuration comes only from the environment; credentials never leave ``ExotelConfig`` (masked ``repr``). Missing
configuration makes the connector unavailable (``unavailable_reason``): the policy never offers voice and the API
reports the transport as unavailable -- a call is never faked. Tests use a fake ``ExotelClient`` (or a patched
``httpx.post``); pytest never reaches Exotel.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, Optional, Protocol, Tuple
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.communication.base import DeliveryConnector, DeliveryRequest, DeliveryResult
from app.communication.contacts import ContactResolver
from app.communication.voice import tokens
from app.communication.voice.audio import SAMPLE_RATE
from app.db.models.communication_delivery import (
    AttemptStatus, CommunicationAttempt, ContactKind, VoiceSession, VoiceSessionStatus,
)
from app.rules.communication_policy import VOICE, CommunicationPolicy

PROVIDER = "exotel"
STATUS_PATH = "/communication/exotel/status"
STREAM_PATH = "/communication/voice/stream"
STREAM_URL_PATH = "/communication/exotel/stream-url"
CALLBACK_VALIDITY = timedelta(hours=2)
CALL_MODES = ("stream", "flow")
DEFAULT_SUBDOMAIN = "api.in.exotel.com"  # Exotel India; api.exotel.com is the Singapore cluster
# A safe, fixed description of each purpose (stored on the VoiceSession; never personal data).
GOALS = {
    "submission_reminder": "Remind the student of a pending assignment and ask whether they will submit it.",
    "deadline_warning": "Warn the student that an assignment deadline is close and ask whether they will submit.",
    "exam_reminder": "Remind the student of an upcoming exam and confirm they are aware.",
    "absence_followup": "Tell the student they were marked absent for an exam and offer help with next steps.",
    "absence_check": "Tell the student an absence was recorded and ask whether it is correct.",
}


@dataclass(frozen=True)
class ExotelConfig:
    account_sid: str = ""
    api_key: str = field(default="", repr=False)
    api_token: str = field(default="", repr=False)
    subdomain: str = DEFAULT_SUBDOMAIN
    caller_id: str = field(default="", repr=False)  # the ExoPhone
    flow_app_id: str = ""  # flow mode only
    public_base_url: str = ""  # where Exotel reaches this API (https; the stream is the same host over wss)
    call_mode: str = "stream"

    @classmethod
    def from_env(cls) -> "ExotelConfig":
        env = os.environ.get
        return cls(account_sid=env("EXOTEL_ACCOUNT_SID", ""), api_key=env("EXOTEL_API_KEY", ""),
                   api_token=env("EXOTEL_API_TOKEN", ""),
                   subdomain=(env("EXOTEL_SUBDOMAIN", "") or DEFAULT_SUBDOMAIN).strip(),
                   caller_id=env("EXOTEL_CALLER_ID", ""), flow_app_id=env("EXOTEL_FLOW_APP_ID", ""),
                   public_base_url=(env("CAMPUSNEXUS_PUBLIC_BASE_URL", "") or "").rstrip("/"),
                   call_mode=(env("EXOTEL_CALL_MODE", "") or "stream").strip().lower())

    def unavailable_reason(self) -> Optional[str]:
        """None when calls can be placed; else a safe code (it names what is missing, never a value)."""
        if self.call_mode not in CALL_MODES:
            return "EXOTEL_CALL_MODE_INVALID"
        if not all((self.account_sid, self.api_key, self.api_token, self.caller_id)):
            return "EXOTEL_NOT_CONFIGURED"
        if self.call_mode == "flow" and not self.flow_app_id:
            return "EXOTEL_FLOW_NOT_CONFIGURED"
        if not self.public_base_url.startswith("https://"):
            return "PUBLIC_WSS_NOT_CONFIGURED"
        return None

    def configured(self) -> bool:
        return self.unavailable_reason() is None

    def flow_url(self) -> str:
        return f"http://my.exotel.com/{self.account_sid}/exoml/start_voice/{self.flow_app_id}"

    def stream_url(self, stream_token: str) -> str:
        """The bidirectional stream URL for one call: wss, linear16 at 16 kHz (``sample-rate=16000``), with the call's
        signed stream token."""
        host = self.public_base_url.split("://", 1)[-1]
        return f"wss://{host}{STREAM_PATH}?sample-rate={SAMPLE_RATE}&token={quote(stream_token)}"


@dataclass(frozen=True)
class ProviderCall:
    reference: str  # Exotel's CallSid
    status: Optional[str] = None


class ExotelError(Exception):
    """A provider failure, as a ``communication_policy`` error code (``RETRYABLE_ERRORS`` decides retries)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExotelClient(Protocol):
    def place_call(self, *, to: str, caller_id: str, status_callback: str, time_limit_seconds: int,
                   stream_url: Optional[str] = None, flow_url: Optional[str] = None,
                   custom_field: Optional[str] = None) -> ProviderCall:
        """Place one outbound call (``stream_url``: Voice AI connect; else ``flow_url``). Raises ``ExotelError``.
        Never logs ``to``."""


_SID_XML = re.compile(r"<Sid>\s*([A-Za-z0-9_-]{1,80})\s*</Sid>")
_STATUS_XML = re.compile(r"<Status>\s*([A-Za-z-]{1,30})\s*</Status>")


def parse_call_response(response: Any) -> ProviderCall:
    """The Call's Sid and status from Exotel's JSON response (or its XML one, should the account answer in XML)."""
    try:
        call = response.json()["Call"]
        return ProviderCall(reference=str(call["Sid"])[:80], status=call.get("Status"))
    except (ValueError, KeyError, TypeError):
        pass
    text = getattr(response, "text", "") or ""
    sid = _SID_XML.search(text)
    if sid is None:
        raise ExotelError("PROVIDER_BAD_RESPONSE")
    status = _STATUS_XML.search(text)
    return ProviderCall(reference=sid.group(1), status=status.group(1) if status else None)


class HttpExotelClient:
    """Exotel's Connect API over httpx (HTTP Basic: API key / API token)."""

    def __init__(self, config: ExotelConfig, timeout: float = 10.0) -> None:
        self.config, self.timeout = config, timeout

    def request_for(self, *, to: str, caller_id: str, status_callback: str, time_limit_seconds: int,
                    stream_url: Optional[str] = None, flow_url: Optional[str] = None,
                    custom_field: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        """(url, form data) of one call. Recording is always off; the number appears only in the returned form."""
        base = f"https://{self.config.subdomain}/v1/Accounts/{self.config.account_sid}/Calls/connect"
        data: Dict[str, Any] = {"From": to, "CallerId": caller_id, "Record": "false",
                                "TimeLimit": str(time_limit_seconds), "StatusCallback": status_callback,
                                "StatusCallbackEvents[]": ["answered", "terminal"]}
        if custom_field:
            data["CustomField"] = custom_field
        if stream_url is not None:  # Connect Voice AI: the call is bridged straight to our bidirectional stream
            data.update(StreamUrl=stream_url, StreamType="bidirectional")
            return base, data
        if flow_url is None:
            raise ExotelError("PROVIDER_REJECTED")
        data["Url"] = flow_url
        return f"{base}.json", data

    def place_call(self, *, to: str, caller_id: str, status_callback: str, time_limit_seconds: int,
                   stream_url: Optional[str] = None, flow_url: Optional[str] = None,
                   custom_field: Optional[str] = None) -> ProviderCall:
        import httpx

        url, data = self.request_for(to=to, caller_id=caller_id, status_callback=status_callback,
                                     time_limit_seconds=time_limit_seconds, stream_url=stream_url, flow_url=flow_url,
                                     custom_field=custom_field)
        try:
            response = httpx.post(url, data=data, auth=(self.config.api_key, self.config.api_token),
                                  timeout=self.timeout, headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            raise ExotelError("PROVIDER_TIMEOUT") from None
        except httpx.HTTPError:
            raise ExotelError("PROVIDER_UNAVAILABLE") from None
        if response.status_code in (401, 403):
            raise ExotelError("PROVIDER_AUTH_FAILED")
        if response.status_code == 429:
            raise ExotelError("PROVIDER_RATE_LIMITED")
        if response.status_code >= 500:
            raise ExotelError("PROVIDER_UNAVAILABLE")
        if response.status_code >= 400:
            raise ExotelError("PROVIDER_REJECTED")
        return parse_call_response(response)


class ExotelVoiceConnector(DeliveryConnector):
    channel, provider = VOICE, PROVIDER

    def __init__(self, config: ExotelConfig, client: Optional[ExotelClient] = None,
                 policy: Optional[CommunicationPolicy] = None, contacts: Optional[ContactResolver] = None) -> None:
        self.config = config
        self.client = client or HttpExotelClient(config)
        self.policy = policy or CommunicationPolicy()
        self.contacts = contacts or ContactResolver()

    def available(self) -> bool:
        return self.config.configured()

    def unavailable_reason(self) -> Optional[str]:
        return self.config.unavailable_reason()

    def deliver(self, session: Session, request: DeliveryRequest) -> DeliveryResult:
        contact = self.contacts.resolve(session, self, request.student_id, ContactKind.PHONE)
        if contact is None or not contact.verified:
            return DeliveryResult("failed", error_code="NO_VERIFIED_CONTACT")
        limit = self.policy.voice_time_limit_seconds
        expires = request.now + timedelta(seconds=limit) + self.policy.callback_grace + CALLBACK_VALIDITY
        callback = tokens.sign("callback", request.organization_id, request.attempt_id, expires)
        stream = tokens.sign("stream", request.organization_id, request.attempt_id, expires)
        route = ({"stream_url": self.config.stream_url(stream)} if self.config.call_mode == "stream"
                 else {"flow_url": self.config.flow_url(), "custom_field": stream})
        try:
            call = self.client.place_call(
                to=contact.address, caller_id=self.config.caller_id,
                status_callback=f"{self.config.public_base_url}{STATUS_PATH}?token={quote(callback)}",
                time_limit_seconds=limit, **route)
        except ExotelError as exc:
            return DeliveryResult("failed", error_code=exc.code[:64])
        session.add(VoiceSession(communication_attempt_id=request.attempt_id, provider_call_reference=call.reference,
                                 status=VoiceSessionStatus.PENDING, max_turns=self.policy.voice_max_turns,
                                 conversation_goal=GOALS.get(request.purpose, "Deliver an institution message.")[:200],
                                 created_at=request.now, updated_at=request.now))
        session.flush()
        return DeliveryResult("in_progress", provider_reference=call.reference)

    def verify(self, session: Session, request: DeliveryRequest, result: DeliveryResult) -> bool:
        """Delivered only when the provider's callback recorded an answered, completed call for this attempt."""
        attempt = session.get(CommunicationAttempt, request.attempt_id)
        voice = session.execute(select(VoiceSession).where(
            VoiceSession.communication_attempt_id == request.attempt_id)).scalars().first()
        return (attempt is not None and voice is not None and attempt.status == AttemptStatus.DELIVERED
                and attempt.answered_at is not None and attempt.provider_reference == result.provider_reference
                and voice.provider_call_reference == attempt.provider_reference
                and voice.status == VoiceSessionStatus.COMPLETED)
