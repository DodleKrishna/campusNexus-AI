"""Communication policy (AgentOS V2 Phase 5): plain, deterministic Python. No LLM, no database.

Everything about *whether* and *how* a student may be contacted is decided here; the Communication Agent's model
only picks among what this allows, and a voice conversation only proposes an outcome code from a fixed list.

* ``authorization_for`` -- routine Guardian reminders (the purposes in ``ROUTINE_PURPOSES``, raised by a Guardian
  that a staff action created) run under *standing approval*: the staff action plus this policy. Anything else
  (unknown purpose, a ``nexus`` source) needs a person's explicit approval before any delivery.
* ``permitted_channels`` -- purpose allowlist AND the student's consent AND provider availability, in a fixed order
  (``CHANNEL_ORDER``). In-app is the guaranteed channel: it needs no credentials and is always available.
* ``check_delivery`` -- the pre-delivery gate, in order (first refusal wins): source still open, authorization,
  channel permitted, attempts left, not before ``not_before`` / backoff, quiet hours (every channel except in-app).
* ``after_failed_attempt`` -- bounded retries with exponential backoff; a non-retryable error fails the job at
  once. A failed channel is never switched silently: ``fallback_channel`` returns another channel only when the
  policy explicitly allows fallback.
* ``map_exotel_status`` -- provider call status -> internal attempt status / error code.
* ``outcome_requires_review`` -- which recipient responses notify the responsible staff member (a dispute, a
  request for help or a callback). A response never changes attendance, a submission or an exam record.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import FrozenSet, Iterable, List, Optional, Tuple

from app.rules.followup_policy import FollowupPolicy, in_quiet_hours, quiet_hours_end

IN_APP, EMAIL, VOICE, SMS, WHATSAPP = "in_app", "email", "voice", "sms", "whatsapp"
CHANNEL_ORDER: Tuple[str, ...] = (IN_APP, EMAIL, VOICE, SMS, WHATSAPP)
QUIET_EXEMPT = frozenset({IN_APP})  # a silent in-app notice is not an interruption
# A Guardian's requested channel name -> the communication channel.
REQUESTED_ALIASES = {"in_app": IN_APP, "email": EMAIL, "voice_call": VOICE, "voice": VOICE, "sms": SMS,
                     "whatsapp": WHATSAPP}

SOURCE_TYPES = ("assignment", "exam", "attendance", "nexus")
# Routine reminders a Guardian may send under standing approval, and the channels each may use.
ROUTINE_PURPOSES = {
    "submission_reminder": frozenset({IN_APP, EMAIL, VOICE}),
    "deadline_warning": frozenset({IN_APP, EMAIL, VOICE}),
    "exam_reminder": frozenset({IN_APP, EMAIL, VOICE}),
    "absence_followup": frozenset({IN_APP, EMAIL, VOICE}),
    "absence_check": frozenset({IN_APP, EMAIL, VOICE}),
}
NON_ROUTINE_CHANNELS = frozenset({IN_APP, EMAIL})  # approved non-routine messages are never a phone call


class VoiceOutcome(str, enum.Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    WILL_SUBMIT = "WILL_SUBMIT"
    NEEDS_HELP = "NEEDS_HELP"
    DISPUTES_STATUS = "DISPUTES_STATUS"
    REQUESTS_CALLBACK = "REQUESTS_CALLBACK"
    UNAVAILABLE = "UNAVAILABLE"
    NO_RESPONSE = "NO_RESPONSE"
    OTHER_SAFE = "OTHER_SAFE"


ACKNOWLEDGING_OUTCOMES = frozenset({VoiceOutcome.ACKNOWLEDGED.value, VoiceOutcome.WILL_SUBMIT.value})
REVIEW_OUTCOMES = frozenset({VoiceOutcome.NEEDS_HELP.value, VoiceOutcome.DISPUTES_STATUS.value,
                             VoiceOutcome.REQUESTS_CALLBACK.value})
DELIVERED_IN_APP = "DELIVERED_IN_APP"
DELIVERED_EMAIL = "DELIVERED_EMAIL"

# Attempt errors that may be retried (after backoff, same channel); every other error fails the job.
RETRYABLE_ERRORS = frozenset({"NO_ANSWER", "BUSY", "PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE",
                              "PROVIDER_RATE_LIMITED", "CALL_FAILED", "CALLBACK_TIMEOUT"})


@dataclass(frozen=True)
class CommunicationPolicy:
    max_attempts: int = 3  # per job (one Guardian follow-up)
    backoff: timedelta = timedelta(minutes=30)  # first retry; doubles per attempt
    max_backoff: timedelta = timedelta(hours=6)
    allow_channel_fallback: bool = False  # never switch channel after a failure unless explicitly allowed
    contact: FollowupPolicy = field(default_factory=FollowupPolicy)  # quiet hours (+ timezone) of the institution
    voice_max_turns: int = 6
    voice_time_limit_seconds: int = 180
    callback_grace: timedelta = timedelta(minutes=5)  # a voice attempt with no final callback after this fails

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be 1-10")
        if not 1 <= self.voice_max_turns <= 12:
            raise ValueError("voice_max_turns must be 1-12")
        if not 30 <= self.voice_time_limit_seconds <= 600:
            raise ValueError("voice_time_limit_seconds must be 30-600")
        if self.backoff <= timedelta(0) or self.max_backoff < self.backoff:
            raise ValueError("invalid backoff")


# --- Authorization and channels --------------------------------------------------------------------------------------


def is_routine(source_type: str, purpose: str) -> bool:
    return source_type in ("assignment", "exam", "attendance") and purpose in ROUTINE_PURPOSES


def authorization_for(source_type: str, purpose: str) -> str:
    return "standing_policy" if is_routine(source_type, purpose) else "approval_required"


def normalize_channel(requested: Optional[str]) -> Optional[str]:
    return REQUESTED_ALIASES.get(requested) if requested else None


@dataclass(frozen=True)
class Consent:
    """A student's consent (``communication_preferences`` or the institution default when there is no row)."""

    in_app: bool = True
    email: bool = True
    voice: bool = False
    sms: bool = False
    whatsapp: bool = False
    preferred: Optional[str] = None
    quiet_start: Optional[time] = None
    quiet_end: Optional[time] = None

    def allows(self, channel: str) -> bool:
        return bool(getattr(self, channel, False))


def purpose_channels(source_type: str, purpose: str) -> FrozenSet[str]:
    return ROUTINE_PURPOSES[purpose] if is_routine(source_type, purpose) else NON_ROUTINE_CHANNELS


def permitted_channels(source_type: str, purpose: str, consent: Consent, available: Iterable[str]) -> List[str]:
    """Purpose allowlist AND consent AND provider availability. The preferred channel (if permitted) comes first."""
    allowed = purpose_channels(source_type, purpose)
    ready = set(available) | {IN_APP}  # in-app needs no external provider
    channels = [c for c in CHANNEL_ORDER if c in allowed and consent.allows(c) and c in ready]
    if consent.preferred in channels:
        channels.remove(consent.preferred)
        channels.insert(0, consent.preferred)
    return channels


class ChannelRefusal(str, enum.Enum):
    CHANNEL_NOT_ALLOWED_FOR_PURPOSE = "CHANNEL_NOT_ALLOWED_FOR_PURPOSE"
    CHANNEL_NOT_CONSENTED = "CHANNEL_NOT_CONSENTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    CHANNEL_SWITCH_NOT_ALLOWED = "CHANNEL_SWITCH_NOT_ALLOWED"


def check_channel(channel: str, *, source_type: str, purpose: str, consent: Consent, available: Iterable[str],
                  previous_channel: Optional[str], previous_failed: bool,
                  policy: CommunicationPolicy) -> Optional[str]:
    """None when ``channel`` may be used now, else a refusal code. Order: purpose, consent, provider, switching."""
    if channel not in purpose_channels(source_type, purpose):
        return ChannelRefusal.CHANNEL_NOT_ALLOWED_FOR_PURPOSE.value
    if not consent.allows(channel):
        return ChannelRefusal.CHANNEL_NOT_CONSENTED.value
    if channel != IN_APP and channel not in set(available):
        return ChannelRefusal.PROVIDER_UNAVAILABLE.value
    if previous_channel is not None and channel != previous_channel and previous_failed and not policy.allow_channel_fallback:
        return ChannelRefusal.CHANNEL_SWITCH_NOT_ALLOWED.value
    return None


def fallback_channels(previous_channel: str, permitted: Iterable[str], policy: CommunicationPolicy) -> List[str]:
    """Alternatives after a failed channel -- only when the policy explicitly allows fallback."""
    if not policy.allow_channel_fallback:
        return []
    return [c for c in permitted if c != previous_channel]


# --- The pre-delivery gate ---------------------------------------------------------------------------------------------


class DeliveryRefusal(str, enum.Enum):
    SOURCE_RESOLVED = "SOURCE_RESOLVED"  # cancel the job and close the follow-up: no contact needed
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    NOT_YET = "NOT_YET"  # before not_before / the backoff
    QUIET_HOURS = "QUIET_HOURS"


@dataclass(frozen=True)
class DeliveryCheck:
    allowed: bool
    reason: str  # ALLOWED or a DeliveryRefusal value
    defer_until: Optional[datetime] = None


def _quiet_policy(policy: CommunicationPolicy, consent: Consent) -> FollowupPolicy:
    contact = policy.contact
    if consent.quiet_start is not None and consent.quiet_end is not None:
        return FollowupPolicy(cooldown=contact.cooldown, max_attempts=contact.max_attempts, quiet_start=consent.quiet_start,
                              quiet_end=consent.quiet_end, timezone=contact.timezone)
    return contact


def check_delivery(*, now: datetime, source_open: bool, authorization: str, channel: str, attempts_so_far: int,
                   not_before: Optional[datetime], consent: Consent, policy: CommunicationPolicy) -> DeliveryCheck:
    def refuse(reason: DeliveryRefusal, until: Optional[datetime] = None) -> DeliveryCheck:
        return DeliveryCheck(False, reason.value, until)

    if not source_open:
        return refuse(DeliveryRefusal.SOURCE_RESOLVED)
    if authorization == "approval_required":
        return refuse(DeliveryRefusal.APPROVAL_PENDING)
    if authorization == "rejected":
        return refuse(DeliveryRefusal.APPROVAL_REJECTED)
    if attempts_so_far >= policy.max_attempts:
        return refuse(DeliveryRefusal.MAX_ATTEMPTS_REACHED)
    if not_before is not None and now < not_before:
        return refuse(DeliveryRefusal.NOT_YET, not_before)
    quiet = _quiet_policy(policy, consent)
    if channel not in QUIET_EXEMPT and in_quiet_hours(now, quiet):
        return refuse(DeliveryRefusal.QUIET_HOURS, quiet_hours_end(now, quiet))
    return DeliveryCheck(True, "ALLOWED")


def backoff_after(attempt_number: int, policy: CommunicationPolicy) -> timedelta:
    """Exponential backoff after attempt ``attempt_number`` (1-based), capped."""
    return min(policy.backoff * (2 ** max(attempt_number - 1, 0)), policy.max_backoff)


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    next_attempt_at: Optional[datetime] = None
    reason: str = ""


def after_failed_attempt(*, now: datetime, attempt_number: int, error_code: Optional[str],
                         policy: CommunicationPolicy) -> RetryDecision:
    if error_code not in RETRYABLE_ERRORS:
        return RetryDecision(False, reason="NON_RETRYABLE_ERROR")
    if attempt_number >= policy.max_attempts:
        return RetryDecision(False, reason="MAX_ATTEMPTS_REACHED")
    return RetryDecision(True, now + backoff_after(attempt_number, policy), "RETRY_SCHEDULED")


# --- Provider status mapping and outcomes ----------------------------------------------------------------------------


@dataclass(frozen=True)
class MappedStatus:
    terminal: bool
    answered: bool
    error_code: Optional[str]  # set for an unsuccessful terminal status
    internal: str  # queued | ringing | in_progress | completed | busy | no_answer | failed | cancelled | unknown


_EXOTEL = {
    "queued": MappedStatus(False, False, None, "queued"),
    "ringing": MappedStatus(False, False, None, "ringing"),
    "in-progress": MappedStatus(False, True, None, "in_progress"),
    "answered": MappedStatus(False, True, None, "in_progress"),
    "completed": MappedStatus(True, True, None, "completed"),
    "busy": MappedStatus(True, False, "BUSY", "busy"),
    "no-answer": MappedStatus(True, False, "NO_ANSWER", "no_answer"),
    "failed": MappedStatus(True, False, "CALL_FAILED", "failed"),
    "canceled": MappedStatus(True, False, "CALL_CANCELLED", "cancelled"),
    "cancelled": MappedStatus(True, False, "CALL_CANCELLED", "cancelled"),
}
_UNKNOWN = MappedStatus(False, False, None, "unknown")


def map_exotel_status(status: Optional[str]) -> MappedStatus:
    return _EXOTEL.get((status or "").strip().lower(), _UNKNOWN)


def outcome_requires_review(outcome_code: Optional[str]) -> bool:
    return outcome_code in REVIEW_OUTCOMES


def is_acknowledgement(outcome_code: Optional[str]) -> bool:
    return outcome_code in ACKNOWLEDGING_OUTCOMES


def parse_hhmm(value: Optional[str]) -> Optional[time]:
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None
