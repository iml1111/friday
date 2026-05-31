"""Vendor-specific LLM call configurations (fully separated, no shared base).

This module defines pure dataclasses only and does not import any LLM SDK,
so configs can be created, inspected, and routed without loading a vendor SDK.

Each config declares only the parameters that the vendor actually supports.
Passing an undefined field raises TypeError at construction time (fail-fast).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AnthropicConfig:
    """Call settings for the Anthropic Messages API."""
    max_tokens: int = 16384
    temperature: float | None = None
    thinking_enabled: bool = False
    thinking_budget: int | None = None


@dataclass
class OpenAIConfig:
    """Call settings for the OpenAI Chat Completions API."""
    max_tokens: int = 16384
    temperature: float | None = None
