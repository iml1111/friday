"""verify_image.py — Real-API check for image-bearing tool results.

Confirms what fake-provider tests cannot: that the backend accepts a
tool_result whose content is a [text, image] block array.

  * claude-* models: the model actually sees the image (names its color).
  * gpt-* models: the adapter flattens the image to a text marker; the API
    accepts the request and no base64 reaches the payload.

Usage:
    LLM_MODEL=<model-id> python scripts/verify/verify_image.py

Cost guardrail: max_tokens=256; caller-side turn cap=4; one 32x32 PNG.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import zlib
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ -> import _env
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root -> friday_agent (when not pip-installed)
from _env import create_config, create_provider, resolve_api_key
from friday_agent.api.openai_provider import OpenAIProvider
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState, Terminal
from friday_agent.messages.types import Message, create_user_message
from friday_agent.tools.base import Tool, ToolResult


def _solid_png(rgb: tuple[int, int, int] = (255, 0, 0), size: int = 32) -> str:
    """Base64 PNG of one solid color, built with the stdlib (no imaging dependency)."""
    row = b"\x00" + bytes(rgb) * size  # filter type 0 + RGB pixels

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


IMAGE = {"media_type": "image/png", "data": _solid_png()}


class _NoInput(BaseModel):
    pass


class Snapshot(Tool):
    """Takes a snapshot of the current screen and returns it as an image."""

    name = "snapshot"

    def input_schema(self) -> type[BaseModel]:
        return _NoInput

    async def call(self, args: dict) -> ToolResult:
        return ToolResult(data="Snapshot captured.", image=IMAGE)


class _Recording:
    """Duck-typed LLMProvider wrapper that captures each complete() call's messages."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.config_type = inner.config_type
        self.received_messages: list[list[dict]] = []

    async def complete(self, messages, system_prompt, tools, config):
        self.received_messages.append(messages)
        return await self._inner.complete(
            messages=messages, system_prompt=system_prompt, tools=tools, config=config
        )


def _has_image_result(api_messages: list[dict]) -> bool:
    return any(
        isinstance(block.get("content"), list)
        and any(part.get("type") == "image" for part in block["content"])
        for msg in api_messages
        if msg.get("role") == "user"
        for block in msg["content"]
        if block.get("type") == "tool_result"
    )


async def main() -> int:
    print("=" * 60)
    print("Verification: Image-bearing tool results (Real API)")
    print("=" * 60)

    model = os.environ.get("LLM_MODEL", "")
    if not model:
        sys.exit("Set the LLM_MODEL environment variable to a real model ID.")

    provider = _Recording(create_provider(model, api_key=resolve_api_key(model)))
    engine = FridayAgent(
        provider=provider,
        tools=[Snapshot()],
        system_prompt="Call snapshot exactly once, then answer in one short sentence.",
        config=create_config(model, max_tokens=256),
    )
    state = LoopState(messages=[create_user_message(
        "Take a snapshot and tell me the single dominant color of the image."
    )])

    collected: list[Message] = []
    terminal: Terminal | None = None
    for _ in range(4):  # caller-side turn cap
        outcome = None
        async for item in engine.step(state):
            if isinstance(item, (LoopState, Terminal)):
                outcome = item
            else:
                collected.append(item)
        if isinstance(outcome, Terminal):
            terminal = outcome
            break
        state = outcome

    final_text = " ".join(
        block.text or ""
        for msg in collected if msg.type == "assistant"
        for block in msg.content if block.type == "text"
    )
    print(f"\nfinal answer : {final_text!r}")
    print(f"terminal     : {terminal.reason if terminal else '(turn cap reached)'}")
    if terminal and terminal.error:
        print(f"error        : {terminal.error}")

    checks = {
        "snapshot result sent as a [text, image] tool_result":
            any(_has_image_result(msgs) for msgs in provider.received_messages),
        "API accepted the request (reason == 'completed')":
            terminal is not None and terminal.reason == "completed",
    }
    if model.startswith("claude-"):
        checks["model saw the image (answer mentions red)"] = "red" in final_text.lower()
    else:
        payload = json.dumps([OpenAIProvider._to_openai_messages(msgs, "") for msgs in provider.received_messages])
        checks["OpenAI payload carries the marker, not the base64"] = (
            "[image omitted" in payload and IMAGE["data"] not in payload
        )

    print("\n--- Checklist ---")
    all_pass = True
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_pass = all_pass and ok
    print("\n" + ("=" * 20 + " PASS " + "=" * 20 if all_pass else "=" * 20 + " FAIL " + "=" * 20))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
