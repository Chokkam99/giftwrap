"""OpenAI adapter (chat.completions tool calling).

Env vars
--------
OPENAI_API_KEY      required. Missing key -> LLMConfigError -> clean 503.
OPENAI_MODEL        optional, default "gpt-5.1". Any tool-calling chat model works.
OPENAI_MAX_TOKENS   optional, default 8192 (sent as max_completion_tokens).
OPENAI_BASE_URL     optional, for Azure/proxy/compatible endpoints.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..base import (
    LLMConfigError,
    Msg,
    Provider,
    ToolCall,
    ToolSpec,
    Turn,
    join_text,
)

DEFAULT_MODEL = "gpt-5.1"
DEFAULT_MAX_TOKENS = 8192

_client: Any | None = None


def get_client() -> Any:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise LLMConfigError(
                "OPENAI_API_KEY is not set — add it to .env and restart the server."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMConfigError(f"the openai SDK is not installed: {exc}") from exc
        kwargs: dict[str, Any] = {"api_key": api_key}
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        _client = OpenAI(**kwargs)
    return _client


def _tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _messages(system: str, messages: list[Msg]) -> list[dict]:
    """Neutral history -> OpenAI chat messages (one `tool` message per result)."""
    out: list[dict] = [{"role": "system", "content": system}]
    for msg in messages:
        if msg.role == "tool_result":
            out.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.content or "",
            })
        elif msg.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments or {}),
                        },
                    }
                    for call in msg.tool_calls
                ]
            out.append(entry)
        else:
            out.append({"role": "user", "content": msg.content or ""})
    return out


def _arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OpenAIProvider(Provider):
    name = "openai"
    api_key_env = "OPENAI_API_KEY"

    def complete(self, system: str, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        client = get_client()
        response = client.chat.completions.create(
            model=self.model,
            max_completion_tokens=self.max_tokens,
            messages=_messages(system, messages),
            tools=_tools(tools),
        )
        choice = response.choices[0].message
        calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=_arguments(tc.function.arguments),
            )
            for tc in (choice.tool_calls or [])
            if getattr(tc, "function", None) is not None
        ]
        return Turn(
            text=join_text([choice.content or ""]),
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end",
        )


def build() -> Provider:
    return OpenAIProvider(
        model=os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        max_tokens=int(os.getenv("OPENAI_MAX_TOKENS", DEFAULT_MAX_TOKENS)),
    )
