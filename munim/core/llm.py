"""LLM boundary for Agent Core v2. All model calls go through `AgentLLM`."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

ToolRunner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class AgentResult:
    text: str
    stop_reason: str
    iterations: int
    usage: dict[str, int] = field(default_factory=dict)


class AgentLLM(Protocol):
    async def run_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        execute: ToolRunner,
    ) -> AgentResult: ...


class ClaudeAgentLLM:
    """Manual tool-use loop on the Messages API (we own state and approval gating)."""

    def __init__(
        self,
        *,
        model: str = "claude-haiku-5-5",
        effort: str = "low",
        max_iterations: int = 8,
        max_tokens: int = 4096,
        client: Any | None = None,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(timeout=60.0, max_retries=2)
        self.client = client
        self.model = model
        self.effort = effort
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens

    async def run_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        execute: ToolRunner,
    ) -> AgentResult:
        convo = list(messages)
        usage: dict[str, int] = {}
        text = ""
        stop = "unknown"
        for iteration in range(1, self.max_iterations + 1):
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                messages=convo,
                output_config={"effort": self.effort},
            )
            _add_usage(usage, getattr(response, "usage", None))
            stop = response.stop_reason or "unknown"
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            if stop == "refusal":
                return AgentResult("", stop, iteration, usage)
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if stop != "tool_use" or not tool_uses:
                return AgentResult(text, stop, iteration, usage)
            convo.append({"role": "assistant", "content": response.content})
            results = []
            for block in tool_uses:
                outcome = await execute(block.name, dict(block.input or {}))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _json(outcome),
                        **({"is_error": True} if "error" in outcome else {}),
                    }
                )
            convo.append({"role": "user", "content": results})
        log.warning("agent hit max iterations", extra={"iterations": self.max_iterations})
        return AgentResult(text, "max_iterations", self.max_iterations, usage)


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _add_usage(total: dict[str, int], usage: Any) -> None:
    if usage is None:
        return
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
