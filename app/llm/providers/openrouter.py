"""OpenRouter-backed LLMProvider (CAMPUS AI): OpenAI-compatible Chat Completions with a bounded model chain.

Reuses ``GroqLLMProvider``'s plumbing unchanged -- finite timeout, bounded retries, rate-limit handling, the
concurrency limit, key redaction, truncation refusal, token telemetry -- and changes only what differs:

* endpoint and key: ``https://openrouter.ai/api/v1`` with ``OPENROUTER_API_KEY`` (backend only; it is sent in the
  ``Authorization`` header and never appears in an error, a log line, telemetry or a response);
* model fallback: OpenRouter's own ``models`` list (primary first, at most ``MAX_MODELS``) with provider failover
  left enabled, so a fallback is one bounded server-side decision, never an application retry loop. The model that
  actually answered (the response's ``model``) is exposed as ``last_model_used`` and lands in telemetry;
* structured output: free models do not reliably support forced tool calls, so every structured call asks for a
  JSON object (the schema is in the prompt and, where supported, in ``response_format``). The answer is parsed and
  Pydantic-validated here; nothing malformed is repaired or trusted, including a 200 that carries an error.

Configuration: ``CAMPUSNEXUS_OPENROUTER_MODEL`` (primary) and ``CAMPUSNEXUS_OPENROUTER_FALLBACK_MODELS``
(comma-separated). Defaults: google/gemma-4-26b-a4b-it:free, then nvidia/nemotron-3.5-lightning:free, then
openrouter/free.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from app.llm.base import LLMMalformedOutputError, LLMProviderError
from app.llm.providers.groq import GroqLLMProvider, _inline_refs

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_PRIMARY_MODEL = "google/gemma-4-26b-a4b-it:free"
DEFAULT_FALLBACK_MODELS = ("nvidia/nemotron-3.5-lightning:free", "openrouter/free")
MODEL_ENV = "CAMPUSNEXUS_OPENROUTER_MODEL"
FALLBACK_ENV = "CAMPUSNEXUS_OPENROUTER_FALLBACK_MODELS"
KEY_ENV = "OPENROUTER_API_KEY"
APP_TITLE = "CAMPUS AI"
MAX_MODELS = 3  # primary + two fallbacks: one bounded routing decision on OpenRouter's side
DEFAULT_MAX_RETRIES = 1
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def model_chain(primary: Optional[str] = None, fallbacks: Optional[List[str]] = None) -> List[str]:
    """The configured model chain: primary first, de-duplicated, valid ids only, at most ``MAX_MODELS``."""
    first = (primary or os.environ.get(MODEL_ENV) or DEFAULT_PRIMARY_MODEL).strip()
    if fallbacks is None:
        raw = os.environ.get(FALLBACK_ENV)
        fallbacks = [m.strip() for m in raw.split(",")] if raw is not None else list(DEFAULT_FALLBACK_MODELS)
    chain: List[str] = []
    for model in [first, *fallbacks]:
        if not model or model in chain:
            continue
        if not _MODEL_ID.fullmatch(model):
            raise LLMProviderError(f"Invalid OpenRouter model id in {MODEL_ENV}/{FALLBACK_ENV}.")
        chain.append(model)
    return chain[:MAX_MODELS]


def parse_json_object(text: str, label: str = "OpenRouter") -> Dict[str, Any]:
    try:
        parsed = json.loads(_FENCE.sub("", (text or "").strip()))
    except ValueError:
        raise LLMMalformedOutputError(f"{label} output was not valid JSON.") from None
    if not isinstance(parsed, dict):
        raise LLMMalformedOutputError(f"{label} output was not a JSON object.")
    return parsed


class OpenRouterLLMProvider(GroqLLMProvider):
    name = "openrouter"
    is_live = True
    _label = "OpenRouter"
    _key_env = KEY_ENV

    def __init__(self, *, model: Optional[str] = None, fallback_models: Optional[List[str]] = None,
                 api_key: Optional[str] = None, base_url: Optional[str] = None, max_retries: Optional[int] = None,
                 **kwargs: Any) -> None:
        self._chain = model_chain(model, fallback_models)
        key = api_key or os.environ.get(KEY_ENV) or ""
        if not key and kwargs.get("client") is None:
            raise LLMProviderError(f"CAMPUSNEXUS_LLM_PROVIDER=openrouter requires {KEY_ENV} to be set (backend only).")
        super().__init__(model=self._chain[0], api_key=key or "unused-test-key",
                         base_url=base_url or DEFAULT_BASE_URL,
                         max_retries=DEFAULT_MAX_RETRIES if max_retries is None else max_retries, **kwargs)
        if kwargs.get("client") is None:
            self._client.headers["X-Title"] = APP_TITLE  # attribution only; carries no user data

    @property
    def models(self) -> List[str]:
        return list(self._chain)

    def __repr__(self) -> str:  # never the key
        return f"OpenRouterLLMProvider(models={self._chain!r})"

    # --- Request shape ------------------------------------------------------------------------------------

    def _decorate_body(self, body: Dict[str, Any], json_mode: bool) -> None:
        if len(self._chain) > 1:
            body["models"] = list(self._chain)  # OpenRouter's bounded fallback, in order
        body["provider"] = {"allow_fallbacks": True}  # provider failover stays enabled
        body["reasoning"] = {"exclude": True}  # reasoning is never returned to us

    def _check_payload(self, payload: Any) -> None:
        super()._check_payload(payload)
        error = payload.get("error")
        if error:  # a 200 that is really an error: never trusted as an answer
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, int) and code >= 500:
                raise self._transient("OpenRouter returned an upstream error.", kind="server_error", status_code=code)
            raise LLMMalformedOutputError("OpenRouter returned an error instead of a completion.")
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            if choices[0].get("finish_reason") == "error" or choices[0].get("error"):
                raise LLMMalformedOutputError("OpenRouter completion ended with an error.")

    # --- Structured output as a validated JSON object (no forced tool calls) ----------------------------------

    def _json_call(self, *, max_tokens: int, system: str, content: str, schema: Dict[str, Any], schema_name: str,
                   operation: str) -> Dict[str, Any]:
        instruction = (f"{system}\n\nReturn only one JSON object (no prose, no code fences) that matches this JSON "
                       f"schema:\n{json.dumps(schema, separators=(',', ':'))}")
        message = self._create(
            max_tokens=max_tokens, system=instruction, content=content, operation=operation,
            response_format={"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
        )
        if message.get("tool_calls"):
            raise LLMMalformedOutputError("OpenRouter returned a tool call where a JSON object was required.")
        return parse_json_object(message.get("content") or "")

    def _structured(self, *, max_tokens: int, system: str, content: str, tool_name: str, description: str, schema_cls: type):
        raw = self._json_call(max_tokens=max_tokens, system=f"{system}\n\n{description}", content=content,
                              schema=_inline_refs(schema_cls.model_json_schema()), schema_name=tool_name,
                              operation=tool_name)
        try:
            return schema_cls.model_validate(raw)
        except ValidationError:
            raise LLMMalformedOutputError(f"OpenRouter '{tool_name}' output failed schema validation.") from None

    def complete_json_schema(self, *, system: str, content: str, schema_name: str, schema: Dict[str, Any],
                             max_tokens: int, operation: str) -> Dict[str, Any]:
        return self._json_call(max_tokens=max_tokens, system=system, content=content, schema=schema,
                               schema_name=schema_name, operation=operation)

    def synthesize_grounded_answer(self, prompt: Any) -> Any:
        from app.schemas.grounded import GroundedSynthesis
        from app.services.grounded_answers import SYNTHESIS_SYSTEM_PROMPT, parse_synthesis

        raw = self._json_call(max_tokens=600, system=SYNTHESIS_SYSTEM_PROMPT,
                              content=json.dumps(prompt.payload(), separators=(",", ":"), default=str),
                              schema=GroundedSynthesis.model_json_schema(), schema_name="grounded_answer",
                              operation="synthesize_grounded_answer")
        return parse_synthesis(raw)
