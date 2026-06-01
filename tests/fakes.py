"""FakeLLMProvider — scripted LLMProvider test double.

This file is test infrastructure, not production code.

Design:
- responses: queue of AssistantResponse instances returned in order.
- errors: dict(call_index → Exception) or list[Exception] for error injection.
  dict form raises only at the specified call index, then falls through to responses.
  list form raises in call order (errors[0] on first call, errors[1] on second, …).
- call_count: total number of complete() invocations.
- received_messages: messages passed to each complete() call, indexed by call order.
- received_system_prompts: system_prompt passed to each complete() call.
- received_tools: tools passed to each complete() call.
"""
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Union

from friday_agent.api.provider import (
    AssistantResponse,
    LLMProvider,
)
from friday_agent.memory.store import IndexEntry, MemoryEntry, MemoryStore


class InMemoryStore(MemoryStore):
    """Dict-backed MemoryStore test double: non-persistent, no file I/O."""

    def __init__(self) -> None:
        self._data: dict[str, MemoryEntry] = {}

    async def save(self, entry: MemoryEntry) -> None:
        if entry.updated_at is None:
            entry = replace(entry, updated_at=time.time())
        self._data[entry.name] = entry

    async def read(self, name: str) -> MemoryEntry | None:
        return self._data.get(name)

    async def delete(self, name: str) -> None:
        self._data.pop(name, None)

    async def load_index(self) -> list[IndexEntry]:
        return [
            IndexEntry(name=e.name, description=e.description, type=e.type, updated_at=e.updated_at)
            for e in self._data.values()
        ]


@dataclass
class FakeConfig:
    """Minimal config for tests (independent of any vendor config)."""
    max_tokens: int = 16384
    temperature: float | None = None


class FakeLLMProvider(LLMProvider):
    """Deterministic LLMProvider test double.

    Args:
        responses: Ordered list of AssistantResponse instances to return from complete().
        errors: Error injection.
            - dict[int, Exception] → raise at the given call_index (0-based).
            - list[Exception] → raise in call order; responses queue used after exhaustion.
            - None → no errors.
    """

    config_type = FakeConfig

    def __init__(
        self,
        responses: list[AssistantResponse],
        errors: Union[dict[int, Exception], list[Exception], None] = None,
    ) -> None:
        self._responses: deque[AssistantResponse] = deque(responses)
        # Normalize errors: list → dict(index → error)
        if isinstance(errors, list):
            self._errors: dict[int, Exception] = {i: e for i, e in enumerate(errors)}
        elif isinstance(errors, dict):
            self._errors = dict(errors)
        else:
            self._errors = {}

        # Observation slots
        self.call_count: int = 0
        self.received_messages: list[list[dict]] = []
        self.received_system_prompts: list[str] = []
        self.received_tools: list[list[dict]] = []
        self.received_configs: list[FakeConfig | None] = []

    async def complete(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        config: FakeConfig | None,
    ) -> AssistantResponse:
        """Return scripted responses in order or raise an injected error."""
        call_index = self.call_count
        self.call_count += 1

        self.received_messages.append(messages)
        self.received_system_prompts.append(system_prompt)
        self.received_tools.append(tools)
        self.received_configs.append(config)

        if call_index in self._errors:
            raise self._errors[call_index]

        if not self._responses:
            raise RuntimeError(
                f"FakeLLMProvider: responses queue exhausted at call_index={call_index}. "
                "Supply enough scripted responses."
            )
        return self._responses.popleft()
