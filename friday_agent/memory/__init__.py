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
    MemoryEntry,
    MemoryStore,
    MemoryType,
    build_memory_section,
)
from friday_agent.memory.tool import MemoryDelete, MemoryRead, MemorySave

__all__ = [
    "MemoryStore",
    "FileMemoryStore",
    "MemoryEntry",
    "IndexEntry",
    "MemoryType",
    "MemorySave",
    "MemoryRead",
    "MemoryDelete",
    "MEMORY_INSTRUCTIONS",
    "build_memory_section",
]
