"""ConnectivityService (AgentOS V2 Phase 6): is the cloud reachable from this deployment?

States: ``cloud_reachable``, ``local_only`` or ``unknown``. The probe is one bounded TCP connect to the configured
cloud provider endpoint (``CAMPUSNEXUS_CONNECTIVITY_PROBE_URL``, default the Groq API host) -- never a random public
website, never a request body. It runs in a daemon thread joined with a finite timeout (DNS included), at most one
probe at a time, and its answer is cached (``CAMPUSNEXUS_CONNECTIVITY_CACHE_SECONDS``) so an agent step never waits on
the network more than once per cache window. A probe that does not finish in time is ``unknown``.

``CAMPUSNEXUS_OFFLINE_MODE=1`` is the developer/edge offline mode: the state is always ``local_only`` (no probe) and
``cloud_allowed()`` is False, so no cloud AI, speech or delivery provider is built or called.

Transitions are published per organization as durable domain events (``NETWORK_LOST`` / ``NETWORK_RESTORED``,
subject ``connectivity``) only when the observed state differs from the last published one, so a cron worker that
starts a fresh process every run never spams events. ``unknown`` publishes nothing.
"""
from __future__ import annotations

import enum
import os
import socket
import threading
import time
from datetime import datetime
from typing import Callable, Optional, Tuple
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agentos.events import EventService
from app.agentos.schemas import DomainEventType
from app.db.models.agent_kernel import DomainEvent

OFFLINE_MODE_ENV = "CAMPUSNEXUS_OFFLINE_MODE"
PROBE_URL_ENV = "CAMPUSNEXUS_CONNECTIVITY_PROBE_URL"
CACHE_ENV = "CAMPUSNEXUS_CONNECTIVITY_CACHE_SECONDS"
TIMEOUT_ENV = "CAMPUSNEXUS_CONNECTIVITY_TIMEOUT_SECONDS"
DEFAULT_PROBE_URL = "https://api.groq.com"
DEFAULT_CACHE_SECONDS, MIN_CACHE_SECONDS, MAX_CACHE_SECONDS = 30.0, 5.0, 300.0
DEFAULT_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS = 2.0, 0.2, 5.0
SUBJECT_CONNECTIVITY, SUBJECT_ID = "connectivity", "cloud"
_TRANSITION_EVENTS = (DomainEventType.NETWORK_LOST.value, DomainEventType.NETWORK_RESTORED.value)


class ConnectivityState(str, enum.Enum):
    CLOUD_REACHABLE = "cloud_reachable"
    LOCAL_ONLY = "local_only"
    UNKNOWN = "unknown"


STATUS_LABELS = {ConnectivityState.CLOUD_REACHABLE: "online", ConnectivityState.LOCAL_ONLY: "offline",
                 ConnectivityState.UNKNOWN: "unknown"}


def offline_mode() -> bool:
    return (os.environ.get(OFFLINE_MODE_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def cloud_allowed() -> bool:
    """False in the explicit offline mode: no cloud provider may be built or called."""
    return not offline_mode()


def _bounded(name: str, default: float, low: float, high: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    value = float(raw) if raw else default
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low}-{high}")
    return value


def probe_target(url: Optional[str] = None) -> Tuple[str, int]:
    """(host, port) of the configured cloud endpoint. Only https/http URLs without credentials, path tricks or
    query are accepted; this comes from server configuration, never from a request."""
    parts = urlsplit((url or os.environ.get(PROBE_URL_ENV) or DEFAULT_PROBE_URL).strip())
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.query:
        raise ValueError(f"{PROBE_URL_ENV} must be an http(s) URL without credentials or query")
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def tcp_probe(host: str, port: int, timeout: float) -> bool:
    """One TCP connect; nothing is sent. True when the endpoint accepted the connection."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class ConnectivityService:
    """Cached, bounded cloud reachability. ``probe`` is a test seam: ``() -> bool``."""

    def __init__(self, probe: Optional[Callable[[], bool]] = None, *, cache_seconds: float = DEFAULT_CACHE_SECONDS,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS, forced: Optional[ConnectivityState] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._probe, self._forced, self._clock = probe, forced, clock
        self.cache_seconds, self.timeout = cache_seconds, timeout
        self._state, self._checked_at = ConnectivityState.UNKNOWN, None  # type: ConnectivityState, Optional[float]
        self._lock, self._probing = threading.Lock(), False
        self._thread: Optional[threading.Thread] = None  # the last probe thread (may outlive its join timeout)
        self.probes = 0  # how many probes actually ran (tests assert caching)

    @classmethod
    def from_env(cls) -> "ConnectivityService":
        if offline_mode():
            return cls(forced=ConnectivityState.LOCAL_ONLY)
        timeout = _bounded(TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
        cache = _bounded(CACHE_ENV, DEFAULT_CACHE_SECONDS, MIN_CACHE_SECONDS, MAX_CACHE_SECONDS)
        host, port = probe_target()
        return cls(lambda: tcp_probe(host, port, timeout), cache_seconds=cache, timeout=timeout)

    @property
    def forced_offline(self) -> bool:
        return self._forced == ConnectivityState.LOCAL_ONLY

    def state(self) -> ConnectivityState:
        if self._forced is not None:
            return self._forced
        if self._probe is None:
            return ConnectivityState.UNKNOWN
        now = self._clock()
        with self._lock:
            fresh = self._checked_at is not None and now - self._checked_at < self.cache_seconds
            stuck = self._thread is not None and self._thread.is_alive()  # e.g. a DNS lookup still hanging
            if fresh or self._probing or stuck:  # never stack probes: answer with the last known state
                return self._state
            self._probing = True
        try:
            state = self._run_probe()
        finally:
            with self._lock:
                self._probing = False
        with self._lock:
            self._state, self._checked_at = state, self._clock()
            return state

    def _run_probe(self) -> ConnectivityState:
        self.probes += 1
        result: list = []
        thread = threading.Thread(target=lambda: result.append(bool(self._probe())), daemon=True,
                                  name="campusnexus-connectivity-probe")
        self._thread = thread
        thread.start()
        thread.join(self.timeout + 0.5)  # DNS can hang beyond the socket timeout: never wait longer than this
        if not result:
            return ConnectivityState.UNKNOWN
        return ConnectivityState.CLOUD_REACHABLE if result[0] else ConnectivityState.LOCAL_ONLY

    def invalidate(self) -> None:
        """A cloud transport failure was observed: the next ``state()`` probes again (still cached afterwards)."""
        with self._lock:
            self._checked_at = None

    def label(self) -> str:
        return STATUS_LABELS[self.state()]


PHASE6_ENVS = ("CAMPUSNEXUS_INTELLIGENCE_MODE", "CAMPUSNEXUS_SPEECH_MODE", PROBE_URL_ENV)


def connectivity_from_env() -> ConnectivityService:
    """The deployment's service. Forced offline in the offline mode; probing only when a Phase 6 routing mode (or a
    probe URL) is configured; otherwise never checked (``unknown``, no network access)."""
    if offline_mode() or any((os.environ.get(name) or "").strip() for name in PHASE6_ENVS):
        return ConnectivityService.from_env()
    return ConnectivityService(None)


def last_published(session: Session) -> Optional[str]:
    """The newest NETWORK_LOST / NETWORK_RESTORED event type of the session's organization (tenant-filtered)."""
    return session.execute(select(DomainEvent.event_type).where(
        DomainEvent.subject_type == SUBJECT_CONNECTIVITY, DomainEvent.event_type.in_(_TRANSITION_EVENTS),
    ).order_by(DomainEvent.id.desc()).limit(1)).scalars().first()


def publish_transition(session: Session, state: ConnectivityState, now: datetime,
                       events: Optional[EventService] = None) -> Optional[DomainEventType]:
    """Publish NETWORK_LOST / NETWORK_RESTORED only when ``state`` differs from the last published transition.
    No event before the first loss (online is the assumed default); ``unknown`` never publishes."""
    last = last_published(session)
    if state == ConnectivityState.LOCAL_ONLY and last != DomainEventType.NETWORK_LOST.value:
        kind = DomainEventType.NETWORK_LOST
    elif state == ConnectivityState.CLOUD_REACHABLE and last == DomainEventType.NETWORK_LOST.value:
        kind = DomainEventType.NETWORK_RESTORED
    else:
        return None
    (events or EventService()).publish(session, event_type=kind, payload={"state": state.value},
                                       subject_type=SUBJECT_CONNECTIVITY, subject_id=SUBJECT_ID, now=now)
    return kind
