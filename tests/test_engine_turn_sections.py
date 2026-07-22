"""FridayAgent.step() turn_sections hook.

Unlike system_sections (static — joined into the system prefix), turn_sections
are rendered every turn and forwarded via run_one_turn(turn_reminders=...) —
the hook for content that changes during a session, riding messages[-1] instead
of the system prompt so the cache prefix survives.

Capture point: engine.run_one_turn stubbed; forwarded kwargs recorded.
"""
import pytest

from friday_agent.api.configs import AnthropicConfig
from friday_agent.api.provider import LLMProvider
from friday_agent.core.state import LoopState
from friday_agent.memory.store import MemoryEntry, MemoryType

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
async def test_default_turn_sections_pass_empty_reminders(monkeypatch):
    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS", memory=InMemoryStore(),
    )
    captured = await _capture_run_one_turn_kwargs(agent, monkeypatch)
    assert captured.get("turn_reminders") == []


@pytest.mark.asyncio
async def test_turn_sections_rendered_in_order(monkeypatch):
    async def aaa() -> str:
        return "AAA"

    async def bbb() -> str:
        return "BBB"

    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS", memory=InMemoryStore(),
        turn_sections=[aaa, bbb],
    )
    captured = await _capture_run_one_turn_kwargs(agent, monkeypatch)
    assert captured["turn_reminders"] == ["AAA", "BBB"]


@pytest.mark.asyncio
async def test_memory_reminder_rides_turn_reminders_before_sections(monkeypatch):
    store = InMemoryStore()
    await store.save(MemoryEntry(name="user-role", description="recruiter", type=MemoryType.user, body="B"))

    async def extra() -> str:
        return "EXTRA-REMINDER"

    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS", memory=store,
        turn_sections=[extra],
    )
    captured = await _capture_run_one_turn_kwargs(agent, monkeypatch)
    reminders = captured["turn_reminders"]
    assert len(reminders) == 2
    assert "user-role" in reminders[0]           # memory index first
    assert reminders[1] == "EXTRA-REMINDER"


@pytest.mark.asyncio
async def test_empty_memory_reminder_is_filtered(monkeypatch):
    # Empty store -> no memory reminder entry (no empty strings forwarded).
    async def extra() -> str:
        return "EXTRA-REMINDER"

    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS", memory=InMemoryStore(),
        turn_sections=[extra],
    )
    captured = await _capture_run_one_turn_kwargs(agent, monkeypatch)
    assert captured["turn_reminders"] == ["EXTRA-REMINDER"]


@pytest.mark.asyncio
async def test_turn_sections_do_not_touch_system_prompt(monkeypatch):
    # The core cache invariant: dynamic content never joins the system prefix.
    async def dyn() -> str:
        return "DYNAMIC-CONTENT"

    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS", memory=InMemoryStore(),
        turn_sections=[dyn],
    )
    captured = await _capture_run_one_turn_kwargs(agent, monkeypatch)
    assert "DYNAMIC-CONTENT" not in captured["system_prompt"]
