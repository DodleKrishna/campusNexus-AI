"""OllamaAgentBrain (AgentOS V2 Phase 6): the local AgentBrain for edge / offline operation.

Same contract as ``GroqAgentBrain``: the model only *describes* the next transition as one ``AgentDecision``; the
Agent Kernel validates and executes it. The request reuses the Phase 2 prompt, the per-transition JSON schema
(``decision_schema``: only offered kinds/tools/delegates) and ``parse_decision`` (the same Pydantic ``AgentDecision``,
unknown keys such as ``reasoning`` refused). No provider tools are sent; a ``tool_calls`` answer is rejected. Ollama's
separate ``thinking`` field is never read: the reply is reduced to its ``content`` immediately and nothing the model
returned (content, thinking, error text) is stored or logged.

Endpoint safety: the base URL comes only from server configuration (``CAMPUSNEXUS_OLLAMA_BASE_URL``, default
``http://127.0.0.1:11434``) and must be loopback or a private-network IP (never link-local / cloud metadata, never
credentials, a path or a query) unless ``CAMPUSNEXUS_OLLAMA_ALLOW_REMOTE_HOST=1`` is set by the operator. The HTTP
client ignores proxy environment variables and never follows redirects, so a local call cannot be routed to a
remote host. Finite timeout, no retries, no model download (``/api/pull`` is never called).

Model selection: ``CAMPUSNEXUS_OLLAMA_MODEL`` (default ``gpt-oss:20b``). A missing model is
``LOCAL_MODEL_UNAVAILABLE``. ``CAMPUSNEXUS_OLLAMA_FALLBACK_MODEL`` is used only when
``CAMPUSNEXUS_OLLAMA_FALLBACK_POLICY=model_unavailable`` explicitly enables it, and only when the primary model is
not installed -- never after a bad answer.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from app.agentos.brain import BrainOutputError, BrainUnavailableError
from app.agentos.providers import (
    MAX_COMPLETION_TOKENS, SYSTEM_PROMPT, _MeteredBrain, brain_payload, decision_schema, parse_decision,
    DEFAULT_MAX_HISTORY, MAX_HISTORY,
)
from app.agentos.schemas import AgentContext, AgentDecision

PROVIDER_ENV = "CAMPUSNEXUS_LOCAL_LLM_PROVIDER"
BASE_URL_ENV = "CAMPUSNEXUS_OLLAMA_BASE_URL"
MODEL_ENV = "CAMPUSNEXUS_OLLAMA_MODEL"
FALLBACK_MODEL_ENV = "CAMPUSNEXUS_OLLAMA_FALLBACK_MODEL"
FALLBACK_POLICY_ENV = "CAMPUSNEXUS_OLLAMA_FALLBACK_POLICY"
TIMEOUT_ENV = "CAMPUSNEXUS_OLLAMA_TIMEOUT_SECONDS"
ALLOW_REMOTE_ENV = "CAMPUSNEXUS_OLLAMA_ALLOW_REMOTE_HOST"
DEFAULT_PROVIDER = "ollama"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gpt-oss:20b"
FALLBACK_ON_MODEL_UNAVAILABLE = "model_unavailable"
DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS = 60.0, 180.0
MAX_MODEL_CHARS = 80
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")  # e.g. gpt-oss:20b, library/qwen3:4b


class LocalEndpointError(ValueError):
    """A configured local endpoint that is not allowed (remote, credentials, path...). ``str`` is a safe code."""


def validate_local_endpoint(url: str, *, allow_remote: bool = False) -> str:
    """The normalized base URL, or ``LocalEndpointError``. Loopback / private IPs only unless ``allow_remote``;
    link-local (cloud metadata), multicast and unspecified addresses are refused even then."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LocalEndpointError("LOCAL_ENDPOINT_INVALID")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise LocalEndpointError("LOCAL_ENDPOINT_INVALID")
    try:
        port = parts.port
    except ValueError:
        raise LocalEndpointError("LOCAL_ENDPOINT_INVALID") from None
    host = parts.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None:
        if host != "localhost" and not allow_remote:
            raise LocalEndpointError("LOCAL_ENDPOINT_NOT_LOCAL")
    elif not ip.is_loopback:
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise LocalEndpointError("LOCAL_ENDPOINT_NOT_ALLOWED")
        if not ip.is_private and not allow_remote:
            raise LocalEndpointError("LOCAL_ENDPOINT_NOT_LOCAL")
    netloc = f"[{host}]" if ip is not None and ip.version == 6 else host
    return f"{parts.scheme}://{netloc}{f':{port}' if port else ''}"


def _model(value: Optional[str], default: Optional[str]) -> Optional[str]:
    name = (value or "").strip() or default
    if name is None:
        return None
    if not _MODEL_NAME.fullmatch(name) or ".." in name:
        raise LocalEndpointError("LOCAL_MODEL_INVALID")
    return name


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    fallback_model: Optional[str] = None  # used only when ``fallback_enabled``
    fallback_enabled: bool = False
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        """Raises ``LocalEndpointError`` / ``ValueError`` on an unusable configuration (reported, never guessed)."""
        env = os.environ.get
        provider = (env(PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()
        if provider != DEFAULT_PROVIDER:
            raise LocalEndpointError("LOCAL_PROVIDER_NOT_SUPPORTED")
        allow_remote = (env(ALLOW_REMOTE_ENV) or "").strip().lower() in ("1", "true", "yes")
        base_url = validate_local_endpoint(env(BASE_URL_ENV) or DEFAULT_BASE_URL, allow_remote=allow_remote)
        raw_timeout = (env(TIMEOUT_ENV) or "").strip()
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        if not 1.0 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"{TIMEOUT_ENV} must be 1-{MAX_TIMEOUT_SECONDS:g}")
        policy = (env(FALLBACK_POLICY_ENV) or "").strip().lower()
        if policy not in ("", "off", "none", FALLBACK_ON_MODEL_UNAVAILABLE):
            raise ValueError(f"{FALLBACK_POLICY_ENV} must be '{FALLBACK_ON_MODEL_UNAVAILABLE}' or off")
        fallback = _model(env(FALLBACK_MODEL_ENV), None)
        return cls(base_url=base_url, model=_model(env(MODEL_ENV), DEFAULT_MODEL) or DEFAULT_MODEL,
                   fallback_model=fallback, fallback_enabled=bool(fallback) and policy == FALLBACK_ON_MODEL_UNAVAILABLE,
                   timeout=timeout)


class OllamaHTTP:
    """Minimal Ollama HTTP plumbing. ``client`` is a test seam (``post(path, json=...)`` / ``get(path)``)."""

    def __init__(self, config: OllamaConfig, client: Any = None) -> None:
        self.config = config
        if client is None:
            import httpx

            # trust_env=False: HTTP(S)_PROXY / ALL_PROXY never reroute local inference to another host.
            client = httpx.Client(base_url=config.base_url, timeout=config.timeout, trust_env=False,
                                  follow_redirects=False)
        self._client = client

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = getattr(self._client, method)(path, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- transport errors are classified, never echoed
            code = "LOCAL_PROVIDER_TIMEOUT" if "Timeout" in type(exc).__name__ else "LOCAL_PROVIDER_UNAVAILABLE"
            raise BrainUnavailableError("local provider unavailable", code=code) from None
        status = response.status_code
        if status == 404:
            raise BrainUnavailableError("local model unavailable", code="LOCAL_MODEL_UNAVAILABLE")
        if 300 <= status < 400:
            raise BrainUnavailableError("local provider redirected", code="LOCAL_PROVIDER_REJECTED")
        if status >= 500:
            raise BrainUnavailableError("local provider error", code="LOCAL_PROVIDER_ERROR")
        if status >= 400:
            raise BrainUnavailableError("local provider rejected the request", code="LOCAL_PROVIDER_REJECTED")
        try:
            body = response.json()
        except ValueError:
            raise BrainOutputError("MALFORMED_OUTPUT") from None
        if not isinstance(body, dict):
            raise BrainOutputError("MALFORMED_OUTPUT")
        return body

    def chat(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("post", "/api/chat", json=body)

    def models(self) -> List[str]:
        """Installed model names (``/api/tags``). Never pulls a model."""
        try:
            body = self._call("get", "/api/tags")
        except BrainOutputError:
            raise BrainUnavailableError("local provider error", code="LOCAL_PROVIDER_ERROR") from None
        return [str(m.get("name") or m.get("model") or "") for m in body.get("models") or [] if isinstance(m, dict)]


def model_installed(names: List[str], model: str) -> bool:
    """Ollama lists ``name:tag``; a configured name without a tag means ``:latest``."""
    wanted = model if ":" in model else f"{model}:latest"
    return model in names or wanted in names


def chat_request(model: str, context: AgentContext, schema: Dict[str, Any], max_history: int) -> Dict[str, Any]:
    content = json.dumps(brain_payload(context, max_history), separators=(",", ":"), default=str)
    return {
        "model": model, "stream": False, "format": schema,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
        "options": {"temperature": 0, "num_predict": MAX_COMPLETION_TOKENS},
    }


def decision_from_reply(body: Dict[str, Any], schema: Dict[str, Any]) -> Tuple[AgentDecision, Tuple[Optional[int], Optional[int]]]:
    """Ollama /api/chat reply -> (AgentDecision, (input_tokens, output_tokens)). Only ``message.content`` is read;
    ``message.thinking`` (and anything else) is dropped here and never leaves this function."""
    message = body.get("message")
    if not isinstance(message, dict):
        raise BrainOutputError("MALFORMED_OUTPUT")
    if message.get("tool_calls"):
        raise BrainOutputError("UNEXPECTED_TOOL_CALL")
    if body.get("done_reason") not in (None, "stop"):
        raise BrainOutputError("TRUNCATED_OUTPUT")
    try:
        raw = json.loads(message.get("content") or "")
    except (TypeError, ValueError):
        raise BrainOutputError("MALFORMED_OUTPUT") from None
    usage = tuple(v if isinstance(v, int) else None for v in (body.get("prompt_eval_count"), body.get("eval_count")))
    return parse_decision(raw, schema), usage  # type: ignore[return-value]


class OllamaAgentBrain(_MeteredBrain):
    provider_name = "ollama"
    is_live = True  # a real model, not the mock
    local = True  # no API cost: telemetry cost stays unavailable, the monetary budget does not gate it

    def __init__(self, config: OllamaConfig, *, http: Any = None, recorder: Any = None,
                 max_history: int = DEFAULT_MAX_HISTORY) -> None:
        super().__init__(recorder)
        self.config = config
        self.http = http if http is not None else OllamaHTTP(config)
        self.max_history = max(1, min(max_history, MAX_HISTORY))
        self._local = threading.local()

    @property
    def model_name(self) -> str:  # type: ignore[override]
        return getattr(self._local, "model", None) or self.config.model

    def available_models(self) -> List[str]:
        return self.http.models()

    def decide(self, context: AgentContext) -> AgentDecision:
        schema = decision_schema(context)
        try:
            return self._decide_with(self.config.model, context, schema)
        except BrainUnavailableError as exc:
            if exc.code != "LOCAL_MODEL_UNAVAILABLE" or not (self.config.fallback_enabled and self.config.fallback_model):
                raise
        return self._decide_with(self.config.fallback_model, context, schema)  # explicit policy: one other model

    def _decide_with(self, model: str, context: AgentContext, schema: Dict[str, Any]) -> AgentDecision:
        self._local.model = model
        started, error_kind, usage, decision = time.perf_counter(), None, None, None
        try:
            body = self.http.chat(chat_request(model, context, schema, self.max_history))
            decision, usage = decision_from_reply(body, schema)
            return decision
        except BrainOutputError as exc:
            error_kind = exc.code
            raise
        except BrainUnavailableError as exc:
            error_kind = exc.code
            raise
        finally:
            self._record(context, started, decision is not None, error_kind, usage, model=model)


def build_local_brain(*, recorder: Any = None, http: Any = None) -> Any:
    """The configured local brain, or an ``UnavailableBrain`` with a safe code (never another provider)."""
    from app.agentos.providers import UnavailableBrain

    try:
        config = OllamaConfig.from_env()
    except LocalEndpointError as exc:
        return UnavailableBrain("ollama", str(exc))
    except ValueError:
        return UnavailableBrain("ollama", "INVALID_LOCAL_BRAIN_CONFIG")
    return OllamaAgentBrain(config, http=http, recorder=recorder)
