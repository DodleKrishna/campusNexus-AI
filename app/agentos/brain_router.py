"""ConnectivityAwareBrainRouter (AgentOS V2 Phase 6): which AgentBrain decides this transition.

``CAMPUSNEXUS_INTELLIGENCE_MODE``:

* ``cloud`` -- the configured cloud brain only (``CAMPUSNEXUS_AGENT_BRAIN``, default ``groq``). Fails explicitly when
  the cloud is unavailable; Ollama is never called.
* ``local`` -- the local Ollama brain only. Zero cloud AI calls.
* ``auto``  -- the cloud brain while the cloud is reachable; the local brain when ``ConnectivityService`` says the
  deployment is ``local_only``, or when the cloud call itself failed in *transport* (``PROVIDER_UNAVAILABLE`` /
  ``PROVIDER_TIMEOUT``: network error, timeout, provider 5xx). Every other cloud failure -- malformed output, a refused
  or unauthorized request, a rate limit, an exhausted AI budget -- is returned as is: it is not connectivity loss and
  never switches to the local model.
* unset -- the Phase 2 behaviour (``build_agent_brain``), unchanged.

Both brains return the same ``AgentDecision`` and the same Agent Kernel validates and executes it: routing changes
who *decides*, never what may be executed. The brain (provider/model) and the route reason of each transition are
exposed per thread (``provider_name`` / ``model_name`` / ``last_route``) and land in the kernel's AI_BRAIN_* audit.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Optional

from app.agentos.brain import BrainUnavailableError
from app.agentos.connectivity import ConnectivityService, ConnectivityState, cloud_allowed
from app.agentos.schemas import AgentContext, AgentDecision

MODE_ENV = "CAMPUSNEXUS_INTELLIGENCE_MODE"
CLOUD, LOCAL, AUTO = "cloud", "local", "auto"
MODES = (CLOUD, LOCAL, AUTO)
CLOUD_BRAINS = frozenset({"groq", "openrouter"})
# Cloud failures that mean "the cloud could not be reached", the only ones that may move a decision to the local brain.
TRANSPORT_CODES = frozenset({"PROVIDER_UNAVAILABLE", "PROVIDER_TIMEOUT"})


class ConnectivityAwareBrainRouter:
    audit_calls = True

    def __init__(self, mode: str, *, connectivity: ConnectivityService, cloud: Any = None, local: Any = None) -> None:
        if mode not in MODES:
            raise ValueError(f"{MODE_ENV} must be one of {', '.join(MODES)}")
        if mode in (CLOUD, AUTO) and cloud is None or mode in (LOCAL, AUTO) and local is None:
            raise ValueError(f"mode {mode} needs its brain(s)")
        self.mode, self.connectivity = mode, connectivity
        self.cloud, self.local = (cloud if mode != LOCAL else None), (local if mode != CLOUD else None)
        self._thread = threading.local()

    # --- What the kernel / assistant / health read ---------------------------------------------------------------

    def _default(self) -> Any:
        return self.local if self.mode == LOCAL else self.cloud

    def _active(self) -> Any:
        return getattr(self._thread, "brain", None) or self._default()

    @property
    def provider_name(self) -> Optional[str]:
        return getattr(self._active(), "provider_name", None)

    @property
    def model_name(self) -> Optional[str]:
        return getattr(self._active(), "model_name", None)

    @property
    def is_live(self) -> bool:
        return bool(getattr(self._active(), "is_live", False))

    @property
    def last_route(self) -> Optional[str]:
        return getattr(self._thread, "route", None)

    @property
    def available(self) -> bool:
        return self.code is None

    @property
    def code(self) -> Optional[str]:
        """None when a decision can be attempted; else the safe refusal code."""
        cloud_ok = self.cloud is not None and getattr(self.cloud, "available", True) and cloud_allowed()
        local_ok = self.local is not None and getattr(self.local, "available", True)
        if self.mode == CLOUD:
            return None if cloud_ok else ("CLOUD_DISABLED_OFFLINE" if not cloud_allowed()
                                          else getattr(self.cloud, "code", "BRAIN_UNAVAILABLE"))
        if self.mode == LOCAL:
            return None if local_ok else getattr(self.local, "code", "LOCAL_BRAIN_UNAVAILABLE")
        return None if cloud_ok or local_ok else getattr(self.local, "code", "BRAIN_UNAVAILABLE")

    def local_available(self) -> bool:
        return self.local is not None and bool(getattr(self.local, "available", True))

    def cloud_available(self) -> bool:
        return (self.cloud is not None and bool(getattr(self.cloud, "available", True)) and cloud_allowed()
                and self.connectivity.state() != ConnectivityState.LOCAL_ONLY)

    # --- Routing -------------------------------------------------------------------------------------------------

    def _use(self, brain: Any, route: str, context: AgentContext) -> Any:
        self._thread.brain, self._thread.route = brain, route
        return brain.decide(context)

    def decide(self, context: AgentContext) -> AgentDecision:
        self._thread.brain, self._thread.route = None, None
        if self.mode == LOCAL:
            return self._use(self.local, "local_mode", context)
        if not cloud_allowed():  # the explicit offline mode refuses every cloud call
            if self.mode == CLOUD:
                raise BrainUnavailableError("cloud disabled in offline mode", code="CLOUD_DISABLED_OFFLINE")
            return self._use(self.local, "offline_mode", context)
        if self.mode == CLOUD:
            return self._use(self.cloud, "cloud_mode", context)
        if self.connectivity.state() == ConnectivityState.LOCAL_ONLY:
            return self._use(self.local, "connectivity_lost", context)
        try:
            return self._use(self.cloud, "cloud_preferred", context)
        except BrainUnavailableError as exc:
            if exc.code not in TRANSPORT_CODES:
                raise  # budget, rate limit, auth, configuration: never a reason to change models
            self.connectivity.invalidate()  # re-probe on the next transition
        return self._use(self.local, "cloud_transport_failed", context)


def intelligence_mode() -> Optional[str]:
    raw = (os.environ.get(MODE_ENV) or "").strip().lower()
    return raw or None


def build_configured_brain(*, recorder: Any = None, connectivity: Optional[ConnectivityService] = None,
                           cloud: Any = None, local: Any = None) -> Any:
    """The deployment's AgentBrain. Mode unset: the Phase 2 ``build_agent_brain`` (unchanged). Otherwise a router
    over the configured cloud brain and/or the local Ollama brain. ``cloud`` / ``local`` are test seams."""
    from app.agentos.local_brain import build_local_brain
    from app.agentos.providers import UnavailableBrain, build_agent_brain

    mode = intelligence_mode()
    if mode is None:
        return build_agent_brain(recorder=recorder)
    if mode not in MODES:
        return UnavailableBrain("router", "INVALID_INTELLIGENCE_MODE")
    connectivity = connectivity or ConnectivityService.from_env()
    if mode in (CLOUD, AUTO) and cloud is None:
        key = (os.environ.get("CAMPUSNEXUS_AGENT_BRAIN") or "groq").strip().lower()
        cloud = (build_agent_brain(key, recorder=recorder) if key in CLOUD_BRAINS
                 else UnavailableBrain(key[:20], "CLOUD_BRAIN_NOT_SUPPORTED"))  # the mock is not a cloud brain
    if mode in (LOCAL, AUTO) and local is None:
        local = build_local_brain(recorder=recorder)
    return ConnectivityAwareBrainRouter(mode, connectivity=connectivity, cloud=cloud, local=local)
