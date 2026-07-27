"""FridayAgent.step() prompt assembly — cache-invariant guards.

Pins the static/dynamic split at the engine boundary: the system prefix step()
assembles is exactly MEMORY_INSTRUCTIONS + system_prompt (byte-stable within a
session), and the live memory index travels only via run_one_turn's
turn_reminders (messages[-1], non-persistent) — never the system prompt, where
a save would invalidate the whole conversation cache.

Capture point: engine.run_one_turn stubbed; forwarded kwargs recorded.
"""
import pytest

from friday_agent.api.configs import AnthropicConfig
from friday_agent.api.provider import LLMProvider
from friday_agent.core.state import LoopState
from friday_agent.memory.store import MEMORY_INSTRUCTIONS, MemoryEntry, MemoryType

import friday_agent.core.engine as engine_mod
from friday_agent.core.engine import FridayAgent
from tests.fakes import InMemoryStore


class _NoopProvider(LLMProvider):
    config_type = AnthropicConfig

    async def complete(self, messages, system_prompt, tools, config):  # pragma: no cover
        raise AssertionError("run_one_turn is stubbed; complete() must not be called")


async def _capture_run_one_turn_kwargs(agent: FridayAgent, monkeypatch) -> dict:
    captured: dict = {}

    async def fake_run_one_turn(**kwargs):
        captured.update(kwargs)
        return
        yield  # noqa: unreachable — makes this an async generator

    monkeypatch.setattr(engine_mod, "run_one_turn", fake_run_one_turn)

    async for _ in agent.step(LoopState(messages=[])):
        pass

    assert captured
    return captured


@pytest.mark.asyncio
async def test_effective_prompt_is_instructions_then_domain_even_with_entries(monkeypatch):
    # Exact equality with a stocked store: proves the assembly shape AND that
    # the index never leaks into the system prefix.
    store = InMemoryStore()
    await store.save(MemoryEntry(name="user-role", description="recruiter", type=MemoryType.user, body="B"))
    agent = FridayAgent(provider=_NoopProvider(), system_prompt="SYS-PROMPT", memory=store)

    captured = await _capture_run_one_turn_kwargs(agent, monkeypatch)

    assert captured["system_prompt"] == f"{MEMORY_INSTRUCTIONS}\n\nSYS-PROMPT"


@pytest.mark.asyncio
async def test_effective_prompt_instructions_only_when_domain_empty(monkeypatch):
    agent = FridayAgent(provider=_NoopProvider(), system_prompt="", memory=InMemoryStore())

    captured = await _capture_run_one_turn_kwargs(agent, monkeypatch)

    assert captured["system_prompt"] == MEMORY_INSTRUCTIONS


@pytest.mark.asyncio
async def test_memory_reminder_forwarded_via_turn_reminders(monkeypatch):
    store = InMemoryStore()
    await store.save(MemoryEntry(name="user-role", description="recruiter", type=MemoryType.user, body="B"))
    agent = FridayAgent(provider=_NoopProvider(), system_prompt="SYS", memory=store)

    captured = await _capture_run_one_turn_kwargs(agent, monkeypatch)

    reminders = captured["turn_reminders"]
    assert len(reminders) == 1
    assert "user-role" in reminders[0]


@pytest.mark.asyncio
async def test_empty_store_forwards_no_reminders(monkeypatch):
    agent = FridayAgent(provider=_NoopProvider(), system_prompt="SYS", memory=InMemoryStore())

    captured = await _capture_run_one_turn_kwargs(agent, monkeypatch)

    assert captured["turn_reminders"] == []
