"""scripts/_env.py — Helper that resolves API keys from the environment.

The friday_agent package never reads env vars directly — API keys are always
injected as constructor arguments. This module owns that "outer injection"
responsibility: it reads the key matching a model's prefix from .env or the
environment and returns it to the caller.

Importing this module calls load_dotenv() once, so any script that imports it
gets .env loaded automatically.
"""
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from friday_agent.api.provider import LLMProvider

load_dotenv()  # load .env if present, silently skip if not

# model prefix → env var name that holds the API key
_KEY_ENV_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("claude-", "ANTHROPIC_API_KEY"),
    ("gpt-", "OPENAI_API_KEY"),
)


def resolve_api_key(model: str) -> str:
    """Return the API key for the given model, resolved from the environment.

    Args:
        model: Vendor model ID. The prefix ("claude-"/"gpt-") determines which
               env var is read.

    Returns:
        The vendor API key string.

    Raises:
        SystemExit: If the prefix is unsupported or the corresponding env var
                    is missing or empty.
    """
    for prefix, env_var in _KEY_ENV_BY_PREFIX:
        if model.startswith(prefix):
            key = os.environ.get(env_var, "")
            if not key:
                sys.exit(f"Set the {env_var} environment variable (required for model {model!r}).")
            return key

    supported = ", ".join(p for p, _ in _KEY_ENV_BY_PREFIX)
    sys.exit(f"Unsupported model prefix: {model!r}. Supported prefixes: {supported}")


# model prefix → provider routing (caller-side convenience; the library ships
# adapter classes directly and does not provide this factory).
_SUPPORTED_PREFIXES: tuple[str, ...] = tuple(prefix for prefix, _ in _KEY_ENV_BY_PREFIX)


def create_provider(model: str, api_key: str, **kwargs) -> "LLMProvider":
    """Instantiate the appropriate LLMProvider for the given model ID.

    Routes on the model-ID prefix and lazy-imports the matching adapter module,
    so using only Claude does not load the OpenAI SDK (and vice versa). This is a
    caller boundary helper — the friday_agent library exposes the adapter classes
    directly and intentionally does not ship a routing factory.

    Args:
        model: Vendor model ID. The prefix determines which backend is used.
        api_key: Vendor API key (required — pair with resolve_api_key(model)).
        **kwargs: Forwarded verbatim to the selected provider constructor
                  (e.g. client).

    Returns:
        LLMProvider instance configured with the given model as default.

    Raises:
        ValueError: If model is empty, not a string, or uses an unsupported prefix.
    """
    if not isinstance(model, str) or not model:
        raise ValueError(
            f"A model ID is required (got {model!r}). "
            f"Supported prefixes: {', '.join(_SUPPORTED_PREFIXES)}"
        )

    if model.startswith("claude-"):
        from friday_agent.api.anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=model, api_key=api_key, **kwargs)

    if model.startswith("gpt-"):
        from friday_agent.api.openai_provider import OpenAIProvider
        return OpenAIProvider(model=model, api_key=api_key, **kwargs)

    raise ValueError(
        f"Unsupported model prefix: {model!r}. "
        f"Supported prefixes: {', '.join(_SUPPORTED_PREFIXES)}"
    )
