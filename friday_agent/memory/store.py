"""Memory data model, MemoryStore ABC, FileMemoryStore backend, and prompt-section assembly.

MemoryStore is the persistence interface for agent memory; FileMemoryStore is the
default single-file Markdown backend. This module also assembles the memory
system-prompt section (static instructions + auto-generated index) the engine
prepends each turn: MEMORY_INSTRUCTIONS, build_memory_section.
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from friday_agent.tools.base import Tool


class MemoryType(str, Enum):
    user = "user"
    feedback = "feedback"
    project = "project"
    reference = "reference"


@dataclass
class MemoryEntry:
    """A full memory (with body); the unit save()/read() operate on."""
    name: str
    description: str
    type: MemoryType
    body: str
    updated_at: float | None = None   # epoch seconds; freshness caveat source


@dataclass
class IndexEntry:
    """Metadata-only memory descriptor for the injected index (no body)."""
    name: str
    description: str
    type: MemoryType
    updated_at: float | None = None


class MemoryStore(ABC):
    """Persistence + tool surface for agent memory (single injection seam)."""

    @abstractmethod
    async def save(self, entry: MemoryEntry) -> None:
        """Upsert by name (same name updates rather than duplicates)."""
        ...

    @abstractmethod
    async def read(self, name: str) -> MemoryEntry | None:
        """Return the full entry (with body), or None if absent."""
        ...

    @abstractmethod
    async def delete(self, name: str) -> None:
        """Remove the entry by name (no error if absent)."""
        ...

    @abstractmethod
    async def load_index(self) -> list["IndexEntry"]:
        """Return metadata-only descriptors (no bodies) for index injection."""
        ...

    def tools(self) -> list["Tool"]:
        """Tools that surface this store to the agent. Default = save/read/delete
        wrapping self. Override to expose a different tool set (e.g. add search)."""
        from friday_agent.memory.tool import MemoryDelete, MemoryRead, MemorySave

        return [MemorySave(self), MemoryRead(self), MemoryDelete(self)]


class FileMemoryStore(MemoryStore):
    """Default backend: a single human-readable Markdown file of memory sections.

    Lazy: reads of an absent file return empty; the file is created on first save().
    A single-process / local-dev convenience — for distributed persistence inject a
    MemoryStore backed by your external Storage.
    Body text must not contain a line matching the entry header pattern '## [type] name'.
    """

    _HEADER = (
        "# FRIDAY_MEMORY.md\n"
        "<!-- Friday agent memory. Auto-managed; safe to read/edit by hand. -->\n"
    )
    _ENTRY_RE = re.compile(r"^##\s+\[(?P<type>\w+)\]\s+(?P<name>.+?)\s*$")

    def __init__(self, path: str = "FRIDAY_MEMORY.md") -> None:
        self._path = Path(path)

    async def save(self, entry: MemoryEntry) -> None:
        if entry.updated_at is None:
            entry = replace(entry, updated_at=time.time())
        entries = self._read_all()
        for idx, existing in enumerate(entries):
            if existing.name == entry.name:
                entries[idx] = entry
                break
        else:
            entries.append(entry)
        self._write_all(entries)

    async def read(self, name: str) -> MemoryEntry | None:
        for e in self._read_all():
            if e.name == name:
                return e
        return None

    async def delete(self, name: str) -> None:
        if not self._path.exists():
            return
        entries = [e for e in self._read_all() if e.name != name]
        self._write_all(entries)

    async def load_index(self) -> list[IndexEntry]:
        return [
            IndexEntry(name=e.name, description=e.description, type=e.type, updated_at=e.updated_at)
            for e in self._read_all()
        ]

    # ── Markdown serialization (this backend's on-disk format) ──────────────────

    def _read_all(self) -> list[MemoryEntry]:
        if not self._path.exists():
            return []
        return self._parse(self._path.read_text(encoding="utf-8"))

    def _write_all(self, entries: list[MemoryEntry]) -> None:
        self._path.write_text(self._serialize(entries), encoding="utf-8")

    @staticmethod
    def _iso(ts: float | None) -> str | None:
        return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _from_iso(s: str) -> float:
        return datetime.fromisoformat(s).timestamp()

    @classmethod
    def _serialize(cls, entries: list[MemoryEntry]) -> str:
        out = [cls._HEADER]
        for e in entries:
            out.append("")
            out.append(f"## [{e.type.value}] {e.name}")
            out.append(f"description: {e.description}")
            iso = cls._iso(e.updated_at)
            if iso:
                out.append(f"updated: {iso}")
            out.append("---")
            out.append(e.body.rstrip("\n"))
        return "\n".join(out) + "\n"

    @classmethod
    def _parse(cls, text: str) -> list[MemoryEntry]:
        lines = text.splitlines()
        entries: list[MemoryEntry] = []
        i, n = 0, len(lines)
        while i < n:
            header = cls._ENTRY_RE.match(lines[i])
            if not header:
                i += 1
                continue
            etype, name = header.group("type"), header.group("name")
            i += 1
            description, updated_at = "", None
            while i < n and lines[i].strip() != "---" and not cls._ENTRY_RE.match(lines[i]):
                line = lines[i]
                if line.startswith("description:"):
                    description = line[len("description:"):].strip()
                elif line.startswith("updated:"):
                    try:
                        updated_at = cls._from_iso(line[len("updated:"):].strip())
                    except ValueError:
                        updated_at = None
                i += 1
            if i < n and lines[i].strip() == "---":
                i += 1
            body_lines: list[str] = []
            while i < n and not cls._ENTRY_RE.match(lines[i]):
                body_lines.append(lines[i])
                i += 1
            try:
                mtype = MemoryType(etype)
            except ValueError:
                continue   # unknown type (e.g. hand-edit typo) — skip rather than crash
            entries.append(
                MemoryEntry(
                    name=name,
                    description=description,
                    type=mtype,
                    body="\n".join(body_lines).strip("\n"),
                    updated_at=updated_at,
                )
            )
        return entries


# --- System-prompt section assembly --------------------------------------------
# Static instructions + auto-generated index. build_memory_section() is async (it
# reads the store's index); the engine builds it every turn and prepends it to the
# base system prompt.

MEMORY_INSTRUCTIONS: str = """# Memory
You have a persistent memory that survives across sessions, accumulated over time.
Use the memory tools to save and recall typed facts.

## Types of memory
 - user: who the user is (role, preferences, expertise).
 - feedback: how the user wants you to work (corrections, confirmed approaches). Include the why.
 - project: ongoing work/goals/constraints not derivable from code or git.
 - reference: pointers to external resources (URLs, dashboards, tickets).

## When to save
Save when you learn a durable fact in one of the four types. For feedback/project,
structure the body as the rule/fact, then **Why:** and **How to apply:** lines.
Reuse an existing name to UPDATE rather than duplicate.

## When to correct
If a saved memory becomes wrong or outdated, fix it the moment you notice — re-save
with the same name to UPDATE it, or use memory_delete to remove it. Never leave a
known-false memory in place.

## What NOT to save
Do not save what is derivable (code structure, architecture, git history), one-off
debugging fixes, or ephemeral conversation/run state.

## When to access
Read a memory when it is relevant or the user asks. If a memory names a file,
function, or flag, verify it still exists before relying on it — a memory saying X
does not guarantee X exists now (if it no longer holds, correct it per "When to
correct"). If the user says to ignore memory, act as if empty.

The memory index below is rebuilt each turn from your saved memories; a memory you
save appears in your tool_result immediately and in the index from the next turn on."""


async def build_memory_section(store: MemoryStore) -> str:
    """Assemble the always-injected memory section: instructions then live index."""
    entries = await store.load_index()
    if entries:
        lines = "\n".join(f"- [{e.type.value}] {e.name} — {e.description}" for e in entries)
        index = f"## Current memory index\n{lines}"
    else:
        index = (
            "## Current memory index\n"
            "Your memory is empty. Save memories as you learn about the user, "
            "their feedback, and the project."
        )
    return f"{MEMORY_INSTRUCTIONS}\n\n{index}"
