"""The provider-neutral contract every LLM adapter implements.

Deliberately tiny: main.py's agent loop only needs "send the whole history plus
tool schemas, get back text and/or tool calls". Anything provider-specific
(wire format, schema dialect, thought signatures) is hidden inside the provider
modules in `llm/providers/`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """The provider call failed."""


class LLMConfigError(LLMError):
    """The provider is not usable: missing API key, missing SDK, bad LLM_PROVIDER.

    main.py turns this into a clean 503 with the message shown to the caller, so
    the message must name the env var that needs setting.
    """


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Provider-private data that must survive a round trip through history
    # (e.g. Gemini 3 thought signatures). Never inspected by main.py.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Msg:
    """One entry of the neutral conversation history.

    role:
      "user"        — buyer text (`content`)
      "assistant"   — model turn: `content` text and/or `tool_calls`
      "tool_result" — one tool's JSON result for `tool_call_id` / `tool_name`
    """

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False


@dataclass
class ToolSpec:
    """A tool the model may call. `parameters` is a JSON-Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class Turn:
    """One model response, normalised."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end"  # "tool_use" | "end"


class Provider:
    """Base class for the three adapters. Subclasses override `complete`."""

    name: str = "unknown"
    api_key_env: str = ""
    # Optional per-provider system-prompt addendum, appended to the shared
    # prompt by main.py. Empty for providers that need no nudging.
    prompt_suffix: str = ""

    def __init__(self, model: str, max_tokens: int) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def key_present(self) -> bool:
        return bool(os.getenv(self.api_key_env))

    def require_key(self) -> str:
        key = os.getenv(self.api_key_env)
        if not key:
            raise LLMConfigError(
                f"{self.api_key_env} is not set — add it to .env and restart the server."
            )
        return key

    def complete(
        self,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec],
    ) -> Turn:
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "key_present": self.key_present()}


def join_text(chunks: list[str]) -> str | None:
    """Shared helper: join the non-empty text pieces of one model turn."""
    kept = [c.strip() for c in chunks if c and c.strip()]
    return "\n\n".join(kept) if kept else None


def group_tool_results(messages: list[Msg]) -> list[list[Msg]]:
    """Split history into runs, batching consecutive tool_result messages.

    Anthropic and Gemini want every tool result from one assistant turn inside a
    single user/function message; OpenAI wants them one per message. Both shapes
    are derivable from these runs.
    """
    runs: list[list[Msg]] = []
    for msg in messages:
        if msg.role == "tool_result" and runs and runs[-1][-1].role == "tool_result":
            runs[-1].append(msg)
        else:
            runs.append([msg])
    return runs
