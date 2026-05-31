"""Abstract LLM backend interface and shared types."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Literal, Protocol, TypeVar, Union, runtime_checkable


@dataclass
class TextBlock:
    """Text response block."""
    type: Literal["text"] = "text"
    text: str = ""


@dataclass
class ToolUseBlock:
    """Tool call block. ``input`` is already parsed to a dict by the SDK."""
    type: Literal["tool_use"] = "tool_use"
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class ThinkingBlock:
    """Extended thinking block (supported by select backends only)."""
    type: Literal["thinking"] = "thinking"
    thinking: str = ""
    signature: str = ""


# ContentBlock union used by AssistantResponse.
# Distinct from the flat ContentBlock dataclass in messages/types.py.
ContentBlock = Union[TextBlock, ToolUseBlock, ThinkingBlock]


class StopReason(str, Enum):
    """Normalized stop signal from the LLM."""
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    CONTEXT_WINDOW_EXCEEDED = "model_context_window_exceeded"


@dataclass
class TokenUsage:
    """Token usage counters. Cache fields are 0 for backends that do not support caching."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class AssistantResponse:
    """Normalized response returned by LLMProvider.complete()."""
    content: list[ContentBlock]
    stop_reason: StopReason
    usage: TokenUsage
    id: str = ""
    model: str = ""


class LLMError(Exception):
    """Base class for all errors raised during LLM calls."""
    pass


class RateLimitError(LLMError):
    """Rate limit reached."""
    pass


class ContextOverflowError(LLMError):
    """Context window exceeded — propagated to the caller to compact (engine.compact) and retry."""
    pass


class AuthError(LLMError):
    """Authentication failure (expired API key, OAuth token, etc.)."""
    pass


class TransientError(LLMError):
    """Transient failure (network timeout, server 5xx, etc.)."""
    pass


@runtime_checkable
class LLMConfig(Protocol):
    """Structural marker satisfied by all vendor configs.

    Only the common levers (max_tokens, temperature) are declared here.
    Vendor-specific options (e.g. thinking) live as explicit fields on each
    concrete config with no shared base class.

    ``@runtime_checkable`` allows isinstance checks to verify that a config
    satisfies this contract without a static type checker installed.
    """
    max_tokens: int
    temperature: float | None


ConfigT = TypeVar("ConfigT", bound=LLMConfig)


class LLMProvider(ABC, Generic[ConfigT]):
    """Abstract interface for an LLM backend.

    Implementations call their vendor's completion API and normalize the
    response to AssistantResponse.

    Subclasses must set ``config_type`` to their vendor config class.
    This is used both for config/provider mismatch validation in QueryEngine
    and for default config creation when no config is supplied
    (``provider.config_type()``).
    """

    # Subclasses set this to their vendor config dataclass (e.g. AnthropicConfig).
    config_type: type[ConfigT]

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        config: ConfigT,
    ) -> "AssistantResponse":
        """Perform a single completion call.

        Args:
            messages: Conversation history. Each element is
                      ``{"role": "user"|"assistant"|"tool", "content": ...}``.
            system_prompt: System prompt text.
            tools: Tool definitions. Each dict must have at least "name",
                   "description", and "input_schema" keys.
            config: Call settings.

        Returns:
            Normalized AssistantResponse.

        Raises:
            LLMError (or subclass): Standard error hierarchy.
        """
        ...
