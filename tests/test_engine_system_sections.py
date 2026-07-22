"""FridayAgent.step() system_sections hook.

Key contract: with no hooks (the default), the effective_prompt step() assembles
is character-identical to the pre-hook assembly (MEMORY_INSTRUCTIONS +
system_prompt) — behavior-neutral. The live memory index rides a turn-local
reminder, never the system prefix (cache-prefix preservation) — see
test_engine_turn_sections.py.

Capture point: the system_prompt step() passes to run_one_turn IS the
effective_prompt. provider.complete()-level capture is unsuitable (the loop
appends further guidance), so run_one_turn is stubbed out in the engine module
and the forwarded system_prompt recorded.
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
    """Satisfies config_type only; complete() must never be reached."""

    config_type = AnthropicConfig

    async def complete(self, messages, system_prompt, tools, config):  # pragma: no cover
        raise AssertionError("run_one_turn is stubbed; complete() must not be called")


async def _capture_effective_prompt(agent: FridayAgent, monkeypatch) -> str:
    """Stub engine.run_one_turn and recover the system_prompt step() forwards."""
    captured: dict[str, str] = {}

    async def fake_run_one_turn(*, system_prompt, **kwargs):
        captured["system_prompt"] = system_prompt
        return
        yield  # noqa: unreachable — makes this an async generator

    monkeypatch.setattr(engine_mod, "run_one_turn", fake_run_one_turn)

    async for _ in agent.step(LoopState(messages=[])):
        pass

    assert "system_prompt" in captured
    return captured["system_prompt"]


@pytest.mark.asyncio
async def test_default_no_system_sections_is_neutral(monkeypatch):
    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS-PROMPT", memory=InMemoryStore(),
    )

    captured = await _capture_effective_prompt(agent, monkeypatch)

    assert captured == f"{MEMORY_INSTRUCTIONS}\n\nSYS-PROMPT"


@pytest.mark.asyncio
async def test_default_neutral_when_system_prompt_empty(monkeypatch):
    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="", memory=InMemoryStore(),
    )

    captured = await _capture_effective_prompt(agent, monkeypatch)

    assert captured == MEMORY_INSTRUCTIONS


@pytest.mark.asyncio
async def test_memory_index_never_in_system_prompt(monkeypatch):
    # The index in the system prefix is what broke the cache on every save.
    store = InMemoryStore()
    await store.save(MemoryEntry(name="user-role", description="recruiter", type=MemoryType.user, body="B"))
    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS-PROMPT", memory=store,
    )

    captured = await _capture_effective_prompt(agent, monkeypatch)

    assert "user-role" not in captured
    assert MEMORY_INSTRUCTIONS in captured


@pytest.mark.asyncio
async def test_system_sections_appended_after_domain_prompt(monkeypatch):
    # Caller sections land after the domain prompt — the most specific layer.
    async def ledger_section() -> str:
        return "SECTION-XYZ"

    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS-PROMPT", memory=InMemoryStore(),
        system_sections=[ledger_section],
    )

    captured = await _capture_effective_prompt(agent, monkeypatch)

    assert captured == f"{MEMORY_INSTRUCTIONS}\n\nSYS-PROMPT\n\nSECTION-XYZ"
    assert captured.index("SECTION-XYZ") > captured.index("SYS-PROMPT")


@pytest.mark.asyncio
async def test_multiple_sections_preserve_order(monkeypatch):
    async def aaa() -> str:
        return "AAA"

    async def bbb() -> str:
        return "BBB"

    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS-PROMPT", memory=InMemoryStore(),
        system_sections=[aaa, bbb],
    )

    captured = await _capture_effective_prompt(agent, monkeypatch)

    assert captured == f"{MEMORY_INSTRUCTIONS}\n\nSYS-PROMPT\n\nAAA\n\nBBB"


@pytest.mark.asyncio
async def test_empty_string_section_is_filtered(monkeypatch):
    async def empty_section() -> str:
        return ""

    agent = FridayAgent(
        provider=_NoopProvider(), system_prompt="SYS-PROMPT", memory=InMemoryStore(),
        system_sections=[empty_section],
    )

    captured = await _capture_effective_prompt(agent, monkeypatch)

    assert captured == f"{MEMORY_INSTRUCTIONS}\n\nSYS-PROMPT"
