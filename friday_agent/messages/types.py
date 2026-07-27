from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class ContentBlock:
    type: str                          # "text" | "tool_use" | "tool_result" | "thinking"
    text: str | None = None            # type=="text"
    id: str | None = None              # type=="tool_use"
    name: str | None = None            # type=="tool_use"
    input: dict | None = None          # type=="tool_use" — SDK already parses this to a dict
    tool_use_id: str | None = None     # type=="tool_result"
    content: str | None = None         # type=="tool_result"
    is_error: bool = False             # type=="tool_result"
    signature: str | None = None       # type=="thinking" — required by the API when echoing thinking back

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "text": self.text,
            "id": self.id,
            "name": self.name,
            "input": self.input,
            "tool_use_id": self.tool_use_id,
            "content": self.content,
            "is_error": self.is_error,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ContentBlock":
        return cls(
            type=d["type"],
            text=d.get("text"),
            id=d.get("id"),
            name=d.get("name"),
            input=d.get("input"),
            tool_use_id=d.get("tool_use_id"),
            content=d.get("content"),
            is_error=d.get("is_error", False),
            signature=d.get("signature"),
        )


@dataclass
class Message:
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    type: str = ""                     # "user" | "assistant" | "system"
    role: str = ""                     # API wire role: "user" | "assistant"
    content: list[ContentBlock] = field(default_factory=list)
    is_api_error_message: bool = False  # synthetic error message; loop branches on this to trigger recovery
    is_compact_summary: bool = False    # message produced by compaction
    is_meta: bool = False               # synthetic message inserted by the system (not sent to the API)

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "type": self.type,
            "role": self.role,
            "content": [b.to_dict() for b in self.content],
            "is_api_error_message": self.is_api_error_message,
            "is_compact_summary": self.is_compact_summary,
            "is_meta": self.is_meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            uuid=d["uuid"],
            type=d["type"],
            role=d["role"],
            content=[ContentBlock.from_dict(b) for b in d.get("content", [])],
            is_api_error_message=d.get("is_api_error_message", False),
            is_compact_summary=d.get("is_compact_summary", False),
            is_meta=d.get("is_meta", False),
        )


def create_user_message(
    content: str | list[ContentBlock],
    *,
    is_meta: bool = False,
    is_compact_summary: bool = False,
) -> Message:
    if isinstance(content, str):
        content = [ContentBlock(type="text", text=content)]
    return Message(
        type="user",
        role="user",
        content=content,
        is_meta=is_meta,
        is_compact_summary=is_compact_summary,
    )


def create_tool_result_message(
    tool_use_id: str,
    result_text: str,
    is_error: bool = False,
) -> Message:
    return Message(
        type="user",
        role="user",
        content=[ContentBlock(
            type="tool_result",
            tool_use_id=tool_use_id,
            content=f"<tool_use_error>{result_text}</tool_use_error>" if is_error else result_text,
            is_error=is_error,
        )],
    )


# Turn-local reminder protocol. Producers (render_todo_reminder in core/loop.py,
# build_memory_reminder in memory/store.py) wrap reminder text via
# wrap_system_reminder; the Anthropic adapter skips blocks starting with
# SYSTEM_REMINDER_PREFIX when placing cache breakpoints — a breakpoint on a
# non-persistent block creates a cache entry that never gets a hit. One
# definition keeps producers and detector from drifting apart.
SYSTEM_REMINDER_PREFIX = "<system-reminder>"


def wrap_system_reminder(body: str) -> str:
    """Wrap turn-local reminder text in the tag pair the cache-control skip detects."""
    return f"{SYSTEM_REMINDER_PREFIX}\n{body}\n</system-reminder>"
