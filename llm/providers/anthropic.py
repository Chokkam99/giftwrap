"""Anthropic adapter — the original behaviour of main.py, ported verbatim.

Env vars
--------
ANTHROPIC_API_KEY   required. Missing key -> LLMConfigError -> clean 503.
ANTHROPIC_MODEL     optional, default "claude-sonnet-5".
ANTHROPIC_MAX_TOKENS optional, default 8192.
"""

from __future__ import annotations

import os
from typing import Any

from ..base import (
    LLMConfigError,
    Msg,
    Provider,
    ToolCall,
    ToolSpec,
    Turn,
    group_tool_results,
    join_text,
)

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 8192

_client: Any | None = None


def get_client() -> Any:
    """Cached anthropic.Anthropic. Module-level cache so tests can reset it."""
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMConfigError(
                "ANTHROPIC_API_KEY is not set — add it to .env and restart the server."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMConfigError(f"the anthropic SDK is not installed: {exc}") from exc
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters}
        for t in tools
    ]


def _messages(messages: list[Msg]) -> list[dict]:
    """Neutral history -> Anthropic messages (unchanged from the pre-adapter wire
    format: assistant content blocks, tool results batched into one user turn)."""
    out: list[dict] = []
    for run in group_tool_results(messages):
        head = run[0]
        if head.role == "tool_result":
            out.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": m.content or "",
                        "is_error": bool(m.is_error),
                    }
                    for m in run
                ],
            })
        elif head.role == "assistant":
            blocks: list[dict] = []
            if head.content:
                blocks.append({"type": "text", "text": head.content})
            for call in head.tool_calls:
                blocks.append({
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                })
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": "user", "content": head.content or ""})
    return out


class AnthropicProvider(Provider):
    name = "anthropic"
    api_key_env = "ANTHROPIC_API_KEY"

    def complete(self, system: str, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        client = get_client()
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=_tools(tools),
            messages=_messages(messages),
        )
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in list(response.content or []):
            kind = getattr(block, "type", None)
            if kind == "text":
                texts.append(block.text)
            elif kind == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input or {}))
            # thinking / unknown blocks are not replayed
        return Turn(
            text=join_text(texts),
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end",
        )


def build() -> Provider:
    return AnthropicProvider(
        model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
        max_tokens=int(os.getenv("ANTHROPIC_MAX_TOKENS", DEFAULT_MAX_TOKENS)),
    )
