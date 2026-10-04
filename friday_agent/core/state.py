"""Loop state dataclasses.

Defines the data structures passed across iterations of the agent loop:
Terminal (loop exit) and LoopState — the serializable loop state, which also
serves as the "continue" sentinel and the transport unit for distributed resume.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from friday_agent.messages.types import Message


# ---------------------------------------------------------------------------
# Terminal — returned when the loop exits
# ---------------------------------------------------------------------------
@dataclass
class Terminal:
    """Returned when the agent loop terminates.

    reason values:
      'completed'    — normal end-turn exit
      'model_error'  — API or network error

    state is the state to persist, always set by the loop: for 'completed', the
    input state plus this turn's assistant message (turn_count + 1); for
    'model_error', the input state itself — step(terminal.state) repeats the
    failed call.
    """
    reason: str
    error: Exception | None = None
    state: LoopState | None = None


# ---------------------------------------------------------------------------
# LoopState — serializable loop state for distributed resume
# ---------------------------------------------------------------------------
@dataclass
class LoopState:
    """Serializable loop state, suitable for distributed resume.

    Carries messages, turn_count, and todos across iterations; run_one_turn()
    yields it directly as the "continue" sentinel at each turn boundary.
    todos is JSON-native structured task state, re-injected into each turn's
    API view as a reminder (see core/loop.py). Non-serializable runtime objects
    (provider/config) are intentionally excluded.
    """
    messages: list[Any]                             # list[Message] (full history)
    turn_count: int = 1
    todos: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "turn_count": self.turn_count,
            "todos": self.todos,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LoopState":
        return cls(
            messages=[Message.from_dict(m) for m in d.get("messages", [])],
            turn_count=d.get("turn_count", 1),
            todos=d.get("todos", []),
        )


# ---------------------------------------------------------------------------
# PendingToolUseError — refusing to send an unpaired tool_use
# ---------------------------------------------------------------------------
class PendingToolUseError(ValueError):
    """Raised when a state with unanswered tool_use blocks would be sent to the model.

    Such a state is a suspended turn waiting for external results: attach them
    with resume() (an is_error result closes a call that will never finish)
    before calling step() or compact(). A ValueError — a caller-side invalid
    state, never an LLMError / model_error.
    """

    def __init__(self, tool_use_ids: list[str]) -> None:
        self.tool_use_ids = tool_use_ids
        super().__init__(
            f"state has tool_use blocks without a tool_result: {tool_use_ids}; "
            "attach their results with resume() first"
        )
