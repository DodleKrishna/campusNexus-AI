"""LLM provider factory.

Resolution order mirrors app/rag/config.py: explicit argument, then the
``CAMPUSNEXUS_LLM_PROVIDER`` environment variable, then a safe offline
default (``mock``) -- so the app and the demo runner work with zero
credentials unless a real provider is explicitly requested.
"""
from __future__ import annotations

import os
from typing import Optional

from app.llm.base import LLMProvider

DEFAULT_PROVIDER = "mock"

_PROVIDER_ALIASES = {"mock", "deterministic", "test"}


def get_llm_provider(provider: Optional[str] = None, **kwargs: object) -> LLMProvider:
    """Build an LLMProvider by name (falls back to CAMPUSNEXUS_LLM_PROVIDER, then "mock").

    ``kwargs`` are forwarded to the real provider's constructor (e.g.
    ``model=``) and ignored for the mock provider.
    """
    key = (provider or os.environ.get("CAMPUSNEXUS_LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()

    if key in _PROVIDER_ALIASES:
        from app.llm.providers.mock import MockLLMProvider

        return MockLLMProvider()

    if key == "anthropic":
        from app.llm.providers.anthropic_provider import AnthropicLLMProvider

        return AnthropicLLMProvider(**kwargs)  # type: ignore[arg-type]

    raise ValueError(f"unknown LLM provider {provider!r}; expected one of {sorted(_PROVIDER_ALIASES | {'anthropic'})}")
