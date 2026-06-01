"""Built-in tools that FridayAgent always registers, even without caller injection.

Populated intentionally (it was previously empty): todo tracking is now an
always-on SDK capability, so its tool ships built-in rather than caller-wired.
"""
from __future__ import annotations

from friday_agent.tools.base import Tool
from friday_agent.tools.builtin.todo_write import TodoWrite


def builtin_tools() -> list[Tool]:
    """Return fresh instances of the tools FridayAgent registers unconditionally."""
    return [TodoWrite()]
