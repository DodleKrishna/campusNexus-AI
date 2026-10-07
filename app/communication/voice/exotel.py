"""Exotel voice provider (AgentOS V2 Phase 5): configuration, the call-placing client and the voice connector.

``ExotelVoiceConnector`` is a ``DeliveryConnector``: it is the only place a phone number is read (through
``ContactResolver``, verified numbers only), and the number goes straight into the provider call -- it is never
stored, logged, audited or returned. One delivery attempt = one outbound call that connects the student to the
Exotel flow (``flow_app_id``), whose Voicebot applet opens the WebSocket stream (``/communication/voice/stream``).
The call is asynchronous: ``deliver`` returns ``in_progress`` and the provider's status callback
(``callbacks.handle_status_callback``) settles the attempt. ``verify`` confirms a delivery only from that recorded
outcome (answered and completed).

Configuration comes only from the environment; credentials never leave ``ExotelConfig`` (masked ``repr``). Missing
configuration makes the connector unavailable, so the policy never offers voice. Tests use a fake ``ExotelClient``;
``HttpExotelClient`` is never called from pytest.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional, Protocol
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.communication.base import DeliveryConnector, DeliveryRequest, DeliveryResult
from app.communication.contacts import ContactResolver
from app.communication.voice import tokens
from app.db.models.communication_delivery import (
    AttemptStatus, CommunicationAttempt, ContactKind, VoiceSession, VoiceSessionStatus,
)
from app.rules.communication_policy import VOICE, CommunicationPolicy

PROVIDER = "exotel"
STATUS_PATH = "/communication/exotel/status"
STREAM_PATH = "/communication/voice/stream"
CALLBACK_VALIDITY = timedelta(hours=2)
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
    subdomain: str = "api.exotel.com"
    caller_id: str = field(default="", repr=False)  # the ExoPhone
    flow_app_id: str = ""
    public_base_url: str = ""  # where Exotel reaches this API (https)

    @classmethod
    def from_env(cls) -> "ExotelConfig":
        env = os.environ.get
        return cls(account_sid=env("EXOTEL_ACCOUNT_SID", ""), api_key=env("EXOTEL_API_KEY", ""),
                   api_token=env("EXOTEL_API_TOKEN", ""), subdomain=env("EXOTEL_SUBDOMAIN", "api.exotel.com"),
                   caller_id=env("EXOTEL_CALLER_ID", ""), flow_app_id=env("EXOTEL_FLOW_APP_ID", ""),
                   public_base_url=(env("CAMPUSNEXUS_PUBLIC_BASE_URL", "") or "").rstrip("/"))

    def configured(self) -> bool:
        return all((self.account_sid, self.api_key, self.api_token, self.caller_id, self.flow_app_id)) \
            and self.public_base_url.startswith("https://")

    def flow_url(self) -> str:
        return f"http://my.exotel.com/{self.account_sid}/exoml/start_voice/{self.flow_app_id}"


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
    def place_call(self, *, to: str, caller_id: str, flow_url: str, status_callback: str, custom_field: str,
                   time_limit_seconds: int) -> ProviderCall:
        """Place one outbound call. Raises ``ExotelError``. Never logs ``to``."""


class HttpExotelClient:
    """Exotel's Connect API over httpx (``POST /v1/Accounts/{sid}/Calls/connect.json``). Not used by pytest."""

    def __init__(self, config: ExotelConfig, timeout: float = 10.0) -> None:
        self.config, self.timeout = config, timeout

    def place_call(self, *, to: str, caller_id: str, flow_url: str, status_callback: str, custom_field: str,
                   time_limit_seconds: int) -> ProviderCall:
        import httpx

        url = f"https://{self.config.subdomain}/v1/Accounts/{self.config.account_sid}/Calls/connect.json"
        data = {"From": to, "CallerId": caller_id, "Url": flow_url, "StatusCallback": status_callback,
                "StatusCallbackEvents[0]": "terminal", "StatusCallbackEvents[1]": "answered",
                "CustomField": custom_field, "TimeLimit": str(time_limit_seconds)}
        try:
            response = httpx.post(url, data=data, auth=(self.config.api_key, self.config.api_token), timeout=self.timeout)
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
        try:
            call = response.json()["Call"]
            return ProviderCall(reference=str(call["Sid"])[:80], status=call.get("Status"))
        except (ValueError, KeyError, TypeError):
            raise ExotelError("PROVIDER_BAD_RESPONSE") from None


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

    def deliver(self, session: Session, request: DeliveryRequest) -> DeliveryResult:
        contact = self.contacts.resolve(session, self, request.student_id, ContactKind.PHONE)
        if contact is None or not contact.verified:
            return DeliveryResult("failed", error_code="NO_VERIFIED_CONTACT")
        limit = self.policy.voice_time_limit_seconds
        expires = request.now + timedelta(seconds=limit) + self.policy.callback_grace + CALLBACK_VALIDITY
        callback = tokens.sign("callback", request.organization_id, request.attempt_id, expires)
        stream = tokens.sign("stream", request.organization_id, request.attempt_id, expires)
        try:
            call = self.client.place_call(
                to=contact.address, caller_id=self.config.caller_id, flow_url=self.config.flow_url(),
                status_callback=f"{self.config.public_base_url}{STATUS_PATH}?token={quote(callback)}",
                custom_field=stream, time_limit_seconds=limit)
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
