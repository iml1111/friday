"""Loop state dataclasses.

Defines the data structures passed across iterations of the agent loop:
Terminal (loop exit), LoopState (serializable subset), and
Checkpoint (serializable transport unit for distributed resume).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from friday_agent.messages.types import Message


# ---------------------------------------------------------------------------
# Terminal — returned when the loop exits
# ---------------------------------------------------------------------------
@dataclass
class Terminal:
    """Returned when the agent loop terminates.

    reason values:
      'completed'       — normal end-turn exit
      'blocking_limit'  — context window exceeded
      'model_error'     — API or network error
      'image_error'     — image processing error
      'hook_stopped'    — hook requested stop
    """
    reason: str
    error: Exception | None = None


# ---------------------------------------------------------------------------
# LoopState — serializable loop state for distributed resume
# ---------------------------------------------------------------------------
@dataclass
class LoopState:
    """Serializable subset of loop state, suitable for distributed resume.

    Carries messages and turn_count across iterations via Checkpoint.
    tool_use_context and pending_tool_use_summary are intentionally excluded:
    they are non-serializable runtime objects.
    """
    messages: list[Any]                             # list[Message] (full history)
    turn_count: int = 1

    def to_dict(self) -> dict:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "turn_count": self.turn_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LoopState":
        return cls(
            messages=[Message.from_dict(m) for m in d.get("messages", [])],
            turn_count=d.get("turn_count", 1),
        )


# ---------------------------------------------------------------------------
# Checkpoint — resumable state at a turn boundary (contrast: Terminal)
# ---------------------------------------------------------------------------
@dataclass
class Checkpoint:
    """Yielded by run_one_turn() when a turn completes and the loop may continue.

    Unlike Terminal, a Checkpoint means the loop has not ended — the embedded
    LoopState is the serializable transport unit for distributed resume. The
    container transport unit is ``json.dumps(checkpoint.to_dict())``.
    """
    state: LoopState

    def to_dict(self) -> dict:
        return {"state": self.state.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        return cls(state=LoopState.from_dict(d["state"]))
