"""Persistent memory subsystem for friday_agent.

A MemoryStore owns both persistence (save/read/delete/load_index) and its agent
tool surface (tools()). FridayAgent registers a default FileMemoryStore unless the
caller injects their own MemoryStore.
"""
from __future__ import annotations

from friday_agent.memory.store import (
    MEMORY_INSTRUCTIONS,
    FileMemoryStore,
    IndexEntry,
    InMemoryStore,
    MemoryEntry,
    MemoryStore,
    MemoryType,
    build_memory_section,
    render_index,
)
from friday_agent.memory.tool import MemoryDelete, MemoryRead, MemorySave

__all__ = [
    "MemoryStore",
    "FileMemoryStore",
    "InMemoryStore",
    "MemoryEntry",
    "IndexEntry",
    "MemoryType",
    "MemorySave",
    "MemoryRead",
    "MemoryDelete",
    "MEMORY_INSTRUCTIONS",
    "render_index",
    "build_memory_section",
]
