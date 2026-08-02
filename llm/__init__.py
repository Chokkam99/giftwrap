"""Provider-agnostic agent brain.

One env var picks the backend:

    LLM_PROVIDER=anthropic | openai | gemini      (default: anthropic)

Each provider module in `llm/providers/` declares its own env vars at the top —
that folder is the per-provider variable map. SDKs are imported lazily inside
the providers, so a missing SDK for a provider you are not using can never
break startup.
"""

from __future__ import annotations

import importlib
import os

from .base import (
    LLMConfigError,
    LLMError,
    Msg,
    Provider,
    ToolCall,
    ToolSpec,
    Turn,
)

__all__ = [
    "LLMConfigError",
    "LLMError",
    "Msg",
    "Provider",
    "ToolCall",
    "ToolSpec",
    "Turn",
    "PROVIDERS",
    "get_provider",
]

# provider name -> module in llm/providers/ exposing `build() -> Provider`
PROVIDERS = {
    "anthropic": "llm.providers.anthropic",
    "openai": "llm.providers.openai",
    "gemini": "llm.providers.gemini",
}

DEFAULT_PROVIDER = "anthropic"


def get_provider(name: str | None = None) -> Provider:
    """Build the provider named by `name` or by $LLM_PROVIDER.

    Importing the provider module does NOT import its SDK (that happens on the
    first call), so this is cheap and safe at startup.
    """
    key = (name or os.getenv("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    module_path = PROVIDERS.get(key)
    if module_path is None:
        raise LLMConfigError(
            f"Unknown LLM_PROVIDER {key!r} — valid values are: "
            f"{', '.join(sorted(PROVIDERS))}."
        )
    module = importlib.import_module(module_path)
    return module.build()
