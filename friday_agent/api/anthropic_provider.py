"""Anthropic LLMProvider adapter.

Wraps the `anthropic` SDK (Messages API) and normalizes responses to the
framework's `AssistantResponse` type. All native SDK exceptions are mapped to
the 5-class error hierarchy (`LLMError` / `RateLimitError` /
`ContextOverflowError` / `AuthError` / `TransientError`).
"""
from __future__ import annotations

import anthropic

from friday_agent.api.configs import AnthropicConfig
from friday_agent.api.provider import (
    AssistantResponse,
    AuthError,
    ContentBlock,
    ContextOverflowError,
    LLMError,
    LLMProvider,
    RateLimitError,
    StopReason,
    TextBlock,
    ThinkingBlock,
    TokenUsage,
    ToolUseBlock,
    TransientError,
)
from friday_agent.messages.types import SYSTEM_REMINDER_PREFIX


# ---------------------------------------------------------------------------
# Mapping constants
# ---------------------------------------------------------------------------
# Vendor stop_reason string → generic StopReason enum.
# SDK observed literals: end_turn / max_tokens / stop_sequence / tool_use / pause_turn / refusal.
_STOP_REASON_MAP: dict[str, StopReason] = {
    "tool_use": StopReason.TOOL_USE,
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    # The SDK signals prompt-too-long as a 400 exception, not a stop_reason.
    # "model_context_window_exceeded" is not in the SDK's Literal but mapped defensively.
    "model_context_window_exceeded": StopReason.CONTEXT_WINDOW_EXCEEDED,
}

# Signals for prompt-too-long / context-window overflow detected via 400 error messages.
# The SDK has no dedicated exception class for this; detection relies on message content.
_CONTEXT_OVERFLOW_SIGNALS: tuple[str, ...] = (
    "prompt is too long",
    "exceed context limit",
    "context window",
    "context_window_exceeded",
    "too many tokens",
)


class AnthropicProvider(LLMProvider[AnthropicConfig]):
    """LLMProvider adapter for the Anthropic Messages API.

    `complete()` calls `AsyncAnthropic().messages.create()` and normalizes the
    response to `AssistantResponse`. Native `anthropic` exceptions are mapped
    to the 5-class hierarchy (`LLMError` / `RateLimitError` /
    `ContextOverflowError` / `AuthError` / `TransientError`).

    Args:
        api_key: Anthropic API key. Required — must be supplied by the caller.
        model: Default model ID (e.g. "claude-sonnet-4-6"). Must be set before
            calling `complete()`.
        client: Injected `AsyncAnthropic` instance for testing or custom
            transports. Created lazily if `None`.

    Raises:
        ValueError: If `api_key` is empty or `None`.
    """

    config_type = AnthropicConfig

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "AnthropicProvider: api_key is required (inject it externally). "
                "Environment-variable fallback has been removed."
            )
        # Never log or print the key; only pass it to the SDK client at creation time.
        self._api_key = api_key
        self._default_model = model or ""
        self._client = client

    # -- client (lazy init) ------------------------------------------------
    @property
    def client(self) -> anthropic.AsyncAnthropic:
        """AsyncAnthropic client, created on first access."""
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    # -- complete ----------------------------------------------------------
    async def complete(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        config: AnthropicConfig | None,
    ) -> AssistantResponse:
        """Execute a single completion call.

        Vendor-specific constraints applied here:
        - Empty `tools` list is omitted entirely (sending `tools=[]` errors on some models).
        - `temperature` is not sent when extended thinking is enabled.

        All exceptions are mapped to the 5-class error hierarchy before re-raising.
        """
        cfg = config or AnthropicConfig()
        params = self._build_params(messages, system_prompt, tools, cfg)
        try:
            native = await self.client.messages.create(**params)
        except Exception as exc:  # noqa: BLE001 — map all native exceptions to 5-class hierarchy
            raise self.map_exception(exc) from exc
        return self.normalize(native)

    def _build_params(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        cfg: AnthropicConfig,
    ) -> dict:
        """Build kwargs for `messages.create()`."""
        # The API expects system as an array of text blocks.
        system_blocks = [{"type": "text", "text": system_prompt}] if system_prompt else []

        params: dict = {
            "model": self._default_model,
            "max_tokens": cfg.max_tokens,
            "system": system_blocks,
            "messages": messages,
            "stream": False,
        }

        # Omit the tools field entirely when the list is empty.
        if tools:
            params["tools"] = tools

        # Extended thinking requires the thinking param and must not include temperature.
        if cfg.thinking_enabled:
            thinking_param: dict = {"type": "enabled"}
            if cfg.thinking_budget is not None:
                thinking_param["budget_tokens"] = cfg.thinking_budget
            params["thinking"] = thinking_param
        elif cfg.temperature is not None:
            params["temperature"] = cfg.temperature

        # Prompt caching is always-on (no opt-out, mirroring OpenAI's automatic
        # caching): place ephemeral breakpoints on the static prefix and the
        # conversation history before sending.
        self._apply_cache_control(params)
        return params

    @staticmethod
    def _is_turn_reminder(block: dict) -> bool:
        # Every turn-local reminder producer (todo in core/loop.py, memory
        # index in memory/store.py) wraps via messages.types.wrap_system_reminder,
        # so the shared SYSTEM_REMINDER_PREFIX is the detection contract.
        return (
            block.get("type") == "text"
            and str(block.get("text", "")).startswith(SYSTEM_REMINDER_PREFIX)
        )

    @classmethod
    def _apply_cache_control(cls, params: dict) -> None:
        """Place ephemeral cache breakpoints on the static prefix (last system
        block — caches tools+system together, since tools render before system)
        and the conversation history (last PERSISTENT block of the final two
        messages). Always within Anthropic's 4-breakpoint budget
        (system + 2 messages = 3).

        Turn-local reminders (`<system-reminder>` blocks, e.g. the todo
        reminder) are skipped when picking the breakpoint block: they vanish
        from that position on the next call, so a breakpoint ON a reminder
        produces a cache entry that never gets a hit — the previous turn's
        new tool_result would then be cache-WRITTEN a second time on the
        following call (measured in production: ~35% of session cache
        writes). Anchoring on the last persistent block keeps the entry
        byte-stable and reusable; the reminders after it are billed as plain
        input (1x, cheaper than a wasted 1.25x write).

        Mutation is safe: system_blocks is built fresh in _build_params and
        messages come from normalize_for_api (fresh dicts, never persisted).
        """
        mark = {"type": "ephemeral"}
        system = params.get("system")
        if system:
            system[-1] = {**system[-1], "cache_control": mark}
        messages = params.get("messages") or []
        for idx in (-1, -2):
            if len(messages) >= abs(idx):
                content = messages[idx].get("content")
                if not (isinstance(content, list) and content):
                    continue
                # Skip trailing reminders (non-persistent); an all-reminder
                # message gets no mark — the messages[-2] anchor covers it.
                i = len(content) - 1
                while i >= 0 and cls._is_turn_reminder(content[i]):
                    i -= 1
                if i >= 0:
                    content[i] = {**content[i], "cache_control": mark}

    # -- normalize ---------------------------------------------------------
    def normalize(self, native_response) -> AssistantResponse:
        """Normalize a native SDK response to `AssistantResponse`.

        Iterates over content blocks and converts each to `TextBlock`,
        `ToolUseBlock`, or `ThinkingBlock`. Unknown block types are skipped.
        """
        content: list[ContentBlock] = []
        for block in getattr(native_response, "content", []) or []:
            normalized = self._normalize_block(block)
            if normalized is not None:
                content.append(normalized)

        return AssistantResponse(
            content=content,
            stop_reason=self._map_stop_reason(getattr(native_response, "stop_reason", None)),
            usage=self._extract_usage(getattr(native_response, "usage", None)),
            id=getattr(native_response, "id", "") or "",
            model=getattr(native_response, "model", "") or "",
        )

    @staticmethod
    def _normalize_block(block) -> ContentBlock | None:
        """Convert a single vendor block to `TextBlock`, `ToolUseBlock`, or `ThinkingBlock`.

        Block types introduced after the adapter was written (e.g. `server_tool_use`,
        `web_search_tool_result`) return `None` and are silently skipped.
        """
        block_type = getattr(block, "type", None)
        if block_type == "text":
            return TextBlock(text=getattr(block, "text", "") or "")
        if block_type == "tool_use":
            # The SDK pre-parses tool_use.input into a dict — do not JSON-parse it again.
            raw_input = getattr(block, "input", None)
            return ToolUseBlock(
                id=getattr(block, "id", "") or "",
                name=getattr(block, "name", "") or "",
                input=raw_input if isinstance(raw_input, dict) else {},
            )
        if block_type == "thinking":
            return ThinkingBlock(
                thinking=getattr(block, "thinking", "") or "",
                signature=getattr(block, "signature", "") or "",
            )
        # Unknown blocks (redacted_thinking, server_tool_use, *_tool_result, etc.) → skip.
        return None

    @staticmethod
    def _map_stop_reason(raw) -> StopReason:
        """Map a vendor stop_reason string to `StopReason`.

        Values not in the mapping (stop_sequence, pause_turn, refusal, None)
        fall back to `END_TURN` — the loop treats them as a non-tool-use turn end.
        """
        if raw is None:
            return StopReason.END_TURN
        return _STOP_REASON_MAP.get(str(raw), StopReason.END_TURN)

    @staticmethod
    def _extract_usage(usage) -> TokenUsage:
        """Map vendor usage object to `TokenUsage`. Missing fields default to 0."""
        if usage is None:
            return TokenUsage()
        return TokenUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )

    # -- exception mapping -------------------------------------------------
    def map_exception(self, exc: Exception) -> LLMError:
        """Map a native anthropic exception to the 5-class LLMError hierarchy.

        Classification (SDK 0.89.0 observed behavior):
          - prompt-too-long / context overflow → ContextOverflowError
            (no dedicated SDK exception; detected via BadRequestError 400 + message content)
          - 401 AuthenticationError            → AuthError
          - 429 RateLimitError                 → RateLimitError
          - APIConnectionError / InternalServerError / 5xx → TransientError
            (Anthropic SDK has no separate APITimeoutError; it is subsumed by APIConnectionError)
          - all other exceptions               → LLMError (base)
        """
        # Pass through if already in the 5-class hierarchy.
        if isinstance(exc, LLMError):
            return exc

        # Checked first: context overflow is signalled as BadRequestError 400.
        if self._is_context_overflow(exc):
            return ContextOverflowError(str(exc))

        if isinstance(exc, anthropic.AuthenticationError):
            return AuthError(str(exc))

        if isinstance(exc, anthropic.RateLimitError):
            return RateLimitError(str(exc))

        if isinstance(exc, (anthropic.APIConnectionError, anthropic.InternalServerError)):
            return TransientError(str(exc))
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status >= 500:
            return TransientError(str(exc))

        return LLMError(str(exc))

    @staticmethod
    def _is_context_overflow(exc: Exception) -> bool:
        """Return whether the exception represents a prompt-too-long / context overflow.

        The Anthropic SDK signals this as `BadRequestError` (HTTP 400) with a
        message like "prompt is too long: ...". Detection checks both status code
        and message/body content, since the SDK exposes the message in both
        `exc.message` and `str(exc)`.
        """
        status = getattr(exc, "status_code", None)
        if status not in (400, 413):  # 400 invalid_request; 413 payload too large (rare)
            return False
        haystacks = [str(exc), str(getattr(exc, "message", "")), str(getattr(exc, "body", ""))]
        text = " ".join(haystacks).lower()
        return any(signal in text for signal in _CONTEXT_OVERFLOW_SIGNALS)
