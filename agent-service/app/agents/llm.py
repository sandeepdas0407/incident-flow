"""Shared Claude call helper: structured JSON output with an optional client-side tool loop.

- Structured output via output_config.format (forced tool_choice is not supported on current models).
- Server-side refusal fallbacks enabled (fallbacks="default"); a full-chain refusal raises LLMRefusal,
  which the orchestrator turns into a hand-off to humans.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

import anthropic

from app.config import get_settings

log = logging.getLogger("incident_flow.llm")

ToolHandler = Callable[[str, dict], Awaitable[str]]


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key or None)
    return _client


async def structured_call(
    *,
    system: str,
    prompt: str,
    schema: dict,
    tools: list[dict] | None = None,
    tool_handler: ToolHandler | None = None,
    max_turns: int = 12,
    max_tokens: int = 16000,
) -> dict:
    s = get_settings()
    messages: list[dict] = [{"role": "user", "content": prompt}]
    kwargs: dict = {}
    if tools:
        kwargs["tools"] = tools

    for _ in range(max_turns):
        response = await client().beta.messages.create(
            model=s.claude_model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": s.claude_effort, "format": {"type": "json_schema", "schema": schema}},
            **kwargs,
        )

        if response.stop_reason == "refusal":
            raise LLMRefusal(f"model declined: {getattr(response, 'stop_details', None)}")
        if response.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens")

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            continue

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason == "tool_use" and tool_uses:
            if tool_handler is None:
                raise LLMError("model requested a tool but no handler was supplied")
            results = []
            for tu in tool_uses:
                try:
                    out = await tool_handler(tu.name, tu.input if isinstance(tu.input, dict) else json.loads(tu.input))
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
                except Exception as e:  # tool errors go back to the model, not up the stack
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"Error: {e}", "is_error": True})
            messages.append({"role": "user", "content": results})
            continue

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMError(f"no text output (stop_reason={response.stop_reason})")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"invalid JSON from model: {e}") from e

    raise LLMError(f"no final answer after {max_turns} turns")
