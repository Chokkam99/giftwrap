"""Gemini adapter — google-genai SDK (the current one, NOT google-generativeai).

Env vars
--------
GEMINI_API_KEY          required. Missing key -> LLMConfigError -> clean 503.
GEMINI_MODEL            optional, default "gemini-2.5-flash" (free-tier workhorse).
GEMINI_MAX_TOKENS       optional, default 8192 output tokens.
GEMINI_THINKING_BUDGET  optional int. Unset = the model's own default. 0 turns
                        thinking off on 2.x models (faster, fewer tokens); do NOT
                        set it on gemini-3.x models, which reject a 0 budget.
GEMINI_RETRIES          optional, default 3 extra attempts on 429/5xx (free-tier
                        rate limits are the main demo hazard).
GEMINI_MAX_RETRY_WAIT   optional, default 30s cap on a single backoff sleep.

Quirks handled here
-------------------
* Gemini's function-declaration schema is a subset of JSON Schema — unsupported
  keywords (additionalProperties, $schema, oneOf, ...) are stripped rather than
  passed through, and a parameters object with no properties is sent as None.
* Function *responses* are matched to calls by NAME, not id, so the neutral
  Msg.tool_name is used (with an id->name fallback scan of history).
* Gemini 3 requires the model's thought_signature to be replayed alongside each
  function call; it is carried in ToolCall.meta and restored on the way back.
* A response can legitimately carry no parts (MAX_TOKENS, safety) — that is
  surfaced as an LLMError instead of being silently turned into empty text.
* The free tier is rate-limited PER MINUTE (observed: 5 requests/min on
  gemini-2.5-flash) and one buyer message costs 2-3 requests, so 429s are
  routine. The 429 body carries the exact wait ("retryDelay": "15s") — that
  value is honoured, because a plain exponential backoff of 1s/2s retries
  straight back into the same closed window and fails the turn.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from ..base import (
    LLMConfigError,
    LLMError,
    Msg,
    Provider,
    ToolCall,
    ToolSpec,
    Turn,
    group_tool_results,
    join_text,
)

log = logging.getLogger("agentic_gifting.llm.gemini")

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_RETRIES = 3
DEFAULT_MAX_RETRY_WAIT = 30.0

# Gemini's Schema keywords. Anything else in a JSON Schema is dropped.
_SCHEMA_KEYS = {
    "type", "format", "title", "description", "nullable", "default", "enum",
    "items", "properties", "required", "minimum", "maximum", "minItems",
    "maxItems", "minLength", "maxLength", "pattern", "anyOf", "propertyOrdering",
}
# Retried: free-tier rate limits and transient backend hiccups.
_RETRY_MARKERS = ("429", "resource_exhausted", "500", "503", "unavailable", "internal")

_client: Any | None = None


def get_client() -> Any:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise LLMConfigError(
                "GEMINI_API_KEY is not set — add it to .env and restart the server."
            )
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMConfigError(f"the google-genai SDK is not installed: {exc}") from exc
        _client = genai.Client(api_key=api_key)
    return _client


def sanitize_schema(schema: Any) -> Any:
    """Recursively drop JSON-Schema keywords Gemini's function declarations reject."""
    if isinstance(schema, list):
        return [sanitize_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: sanitize_schema(v) for k, v in value.items()}
        elif key in ("items", "anyOf"):
            out[key] = sanitize_schema(value)
        else:
            out[key] = value
    return out


def _declarations(tools: list[ToolSpec]) -> list[dict]:
    declarations = []
    for tool in tools:
        params = sanitize_schema(tool.parameters or {})
        # An object schema with no properties is rejected — omit it entirely.
        if not (isinstance(params, dict) and params.get("properties")):
            params = None
        declaration: dict[str, Any] = {"name": tool.name, "description": tool.description}
        if params is not None:
            declaration["parameters"] = params
        declarations.append(declaration)
    return declarations


def _name_for(messages: list[Msg], tool_call_id: str | None) -> str:
    """Fallback for a tool_result that carries an id but no tool_name."""
    for msg in messages:
        for call in msg.tool_calls:
            if call.id == tool_call_id:
                return call.name
    return "unknown_tool"


def _contents(messages: list[Msg], types: Any) -> list[Any]:
    """Neutral history -> Gemini contents/parts."""
    contents: list[Any] = []
    for run in group_tool_results(messages):
        head = run[0]
        if head.role == "tool_result":
            parts = [
                types.Part.from_function_response(
                    name=msg.tool_name or _name_for(messages, msg.tool_call_id),
                    response={"result": msg.content or ""},
                )
                for msg in run
            ]
            contents.append(types.Content(role="user", parts=parts))
        elif head.role == "assistant":
            parts = []
            if head.content:
                parts.append(types.Part(text=head.content))
            for call in head.tool_calls:
                part = types.Part(
                    function_call=types.FunctionCall(
                        id=call.id, name=call.name, args=call.arguments or {}
                    )
                )
                signature = call.meta.get("thought_signature")
                if signature:
                    part.thought_signature = signature
                parts.append(part)
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        else:
            contents.append(
                types.Content(role="user", parts=[types.Part(text=head.content or "")])
            )
    return contents


def _is_retryable(exc: Exception) -> bool:
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(marker in blob for marker in _RETRY_MARKERS)


# "retryDelay": "15s"  /  "Please retry in 15.616716796s."
_DELAY_PATTERNS = (
    re.compile(r"retrydelay['\"]?\s*[:=]\s*['\"]?([\d.]+)s", re.I),
    re.compile(r"retry in\s+([\d.]+)s", re.I),
)


def retry_delay(exc: Exception, attempt: int, cap: float) -> float:
    """How long to wait before retrying: the server's own figure when it gives
    one (a 429 window does not open early), else exponential backoff. +1s of
    slack, capped so a wedged quota cannot stall a request forever."""
    blob = str(exc)
    for pattern in _DELAY_PATTERNS:
        match = pattern.search(blob)
        if match:
            try:
                return min(float(match.group(1)) + 1.0, cap)
            except ValueError:  # pragma: no cover - regex guarantees a number
                break
    return min(2.0 ** attempt, cap)


class GeminiProvider(Provider):
    name = "gemini"
    api_key_env = "GEMINI_API_KEY"
    # Gemini needs the tool contract restated a little more bluntly than Claude:
    # without this it likes to narrate a search it never performed.
    prompt_suffix = (
        "\n\nTOOL DISCIPLINE (important): you have real tools. Never describe, promise or "
        "invent the result of a tool — call the tool and wait for its result. Product titles, "
        "prices, stores, session ids, tokens and order ids may ONLY come from a tool result you "
        "have already received in this conversation. When a step needs a tool, emit the function "
        "call instead of saying you are about to make one."
    )

    def __init__(self, model: str, max_tokens: int, thinking_budget: int | None,
                 retries: int, max_retry_wait: float) -> None:
        super().__init__(model=model, max_tokens=max_tokens)
        self.thinking_budget = thinking_budget
        self.retries = retries
        self.max_retry_wait = max_retry_wait

    def _config(self, system: str, tools: list[ToolSpec], types: Any) -> Any:
        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": self.max_tokens,
            # We run our own tool loop; never let the SDK call functions for us.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if tools:
            kwargs["tools"] = [types.Tool(function_declarations=_declarations(tools))]
        if self.thinking_budget is not None:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self.thinking_budget
            )
        return types.GenerateContentConfig(**kwargs)

    def complete(self, system: str, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        from google.genai import types

        client = get_client()
        config = self._config(system, tools, types)
        contents = _contents(messages, types)

        response = None
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                break
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                if attempt >= self.retries or not _is_retryable(exc):
                    raise LLMError(f"Gemini call failed: {type(exc).__name__}: {exc}") from exc
                delay = retry_delay(exc, attempt, self.max_retry_wait)
                log.warning(
                    "gemini call failed (%s: %s) — retrying in %.1fs (attempt %d/%d)",
                    type(exc).__name__, str(exc)[:200], delay, attempt + 1, self.retries,
                )
                time.sleep(delay)
        if response is None:  # pragma: no cover - loop always breaks or raises
            raise LLMError(f"Gemini call failed: {last_error}")

        candidates = list(response.candidates or [])
        parts = []
        if candidates and candidates[0].content is not None:
            parts = list(candidates[0].content.parts or [])
        if not parts:
            reason = getattr(candidates[0], "finish_reason", None) if candidates else None
            feedback = getattr(response, "prompt_feedback", None)
            raise LLMError(
                f"Gemini returned no content (finish_reason={reason}, feedback={feedback}). "
                "If this says MAX_TOKENS, raise GEMINI_MAX_TOKENS or lower thinking."
            )

        texts: list[str] = []
        calls: list[ToolCall] = []
        for index, part in enumerate(parts):
            if getattr(part, "thought", False):
                continue  # thinking summaries are not shown to the buyer
            call = getattr(part, "function_call", None)
            if call is not None:
                meta = {}
                signature = getattr(part, "thought_signature", None)
                if signature:
                    meta["thought_signature"] = signature
                calls.append(ToolCall(
                    # Gemini often omits the id — synthesise a stable one.
                    id=call.id or f"gem_{len(messages)}_{index}_{call.name}",
                    name=call.name,
                    arguments=dict(call.args or {}),
                    meta=meta,
                ))
            elif getattr(part, "text", None):
                texts.append(part.text)
        return Turn(
            text=join_text(texts),
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end",
        )


def _num_env(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        log.warning("ignoring non-numeric %s=%r", name, raw)
        return None


def _int_env(name: str) -> int | None:
    value = _num_env(name)
    return None if value is None else int(value)


def build() -> Provider:
    retries = _int_env("GEMINI_RETRIES")
    return GeminiProvider(
        model=os.getenv("GEMINI_MODEL", DEFAULT_MODEL),
        max_tokens=_int_env("GEMINI_MAX_TOKENS") or DEFAULT_MAX_TOKENS,
        thinking_budget=_int_env("GEMINI_THINKING_BUDGET"),
        retries=DEFAULT_RETRIES if retries is None else retries,
        max_retry_wait=_num_env("GEMINI_MAX_RETRY_WAIT") or DEFAULT_MAX_RETRY_WAIT,
    )
