"""OpenAI LLMProvider adapter.

Implements the same `LLMProvider` contract as `AnthropicProvider` but targets the
OpenAI Chat Completions API. Key behavioral differences from the Anthropic adapter:

1. Message format conversion (`_to_openai_messages`):
   The internal message dict format follows Anthropic's content-block schema
   (text / tool_use / tool_result blocks, role user|assistant). OpenAI expects:
     - tool calls as `tool_calls` on assistant messages,
     - tool results as separate `{"role": "tool", "tool_call_id", "content"}` messages,
     - the system prompt as a leading `{"role": "system"}` message.
   This adapter performs that conversion.

2. Tool-call arguments: the Anthropic SDK pre-parses `tool_use.input` into a dict,
   but OpenAI returns `tool_calls[].function.arguments` as a **JSON string** that
   must be parsed with `json.loads`.

3. Exception mapping follows the same 5-class hierarchy but uses openai exception
   classes. Context-length overflow maps to `ContextOverflowError`.
"""
from __future__ import annotations

import json

import openai

from friday_agent.api.configs import OpenAIConfig
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
    TokenUsage,
    ToolUseBlock,
    TransientError,
)


# ---------------------------------------------------------------------------
# Mapping constants
# ---------------------------------------------------------------------------
# OpenAI finish_reason → generic StopReason.
# Observed literals: stop / length / tool_calls / content_filter / function_call (legacy).
_FINISH_REASON_MAP: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,  # legacy function-calling
    "length": StopReason.MAX_TOKENS,
    # content_filter / None → END_TURN (treated as a non-tool-use turn end)
}

# Signals for context-length overflow, checked against error code and message text.
_CONTEXT_OVERFLOW_SIGNALS: tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "reduce the length",
    "too many tokens",
)


class OpenAIProvider(LLMProvider[OpenAIConfig]):
    """LLMProvider adapter for the OpenAI Chat Completions API.

    Args:
        api_key: OpenAI API key. Required — must be supplied by the caller.
        model: Default model ID (e.g. "gpt-4o"). Must be set before calling
            `complete()`.
        client: Injected `AsyncOpenAI` instance for testing or custom
            transports. Created lazily if `None`.

    Raises:
        ValueError: If `api_key` is empty or `None`.
    """

    config_type = OpenAIConfig

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        client: "openai.AsyncOpenAI | None" = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "OpenAIProvider: api_key is required (inject it externally). "
                "Environment-variable fallback has been removed."
            )
        self._api_key = api_key
        self._default_model = model or ""
        self._client = client

    # -- client (lazy init) ------------------------------------------------
    @property
    def client(self) -> "openai.AsyncOpenAI":
        """AsyncOpenAI client, created on first access."""
        if self._client is None:
            self._client = openai.AsyncOpenAI(api_key=self._api_key)
        return self._client

    # -- complete ----------------------------------------------------------
    async def complete(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        config: OpenAIConfig | None,
    ) -> AssistantResponse:
        """Execute a single completion call.

        All exceptions are mapped to the 5-class error hierarchy before re-raising.
        """
        cfg = config or OpenAIConfig()
        params = self._build_params(messages, system_prompt, tools, cfg)
        try:
            native = await self.client.chat.completions.create(**params)
        except Exception as exc:  # noqa: BLE001 — map all native exceptions to 5-class hierarchy
            raise self.map_exception(exc) from exc
        return self.normalize(native)

    def _build_params(
        self,
        messages: list[dict],
        system_prompt: str,
        tools: list[dict],
        cfg: OpenAIConfig,
    ) -> dict:
        """Build kwargs for `chat.completions.create()`."""
        params: dict = {
            "model": self._default_model,
            "max_tokens": cfg.max_tokens,
            "messages": self._to_openai_messages(messages, system_prompt),
            "stream": False,
        }

        oa_tools = self._to_openai_tools(tools)
        if oa_tools:  # omit the tools field entirely when the list is empty
            params["tools"] = oa_tools

        if cfg.temperature is not None:
            params["temperature"] = cfg.temperature

        return params

    # -- message / tool conversion (internal Anthropic-format → OpenAI format) --
    @staticmethod
    def _to_openai_messages(messages: list[dict], system_prompt: str) -> list[dict]:
        """Convert internal (Anthropic-format) message dicts to OpenAI Chat Completions messages.

        Conversion rules:
          - system_prompt → leading {"role": "system"} message
          - text blocks   → message content string
          - tool_use blocks (assistant) → assistant.tool_calls (arguments as JSON string)
          - tool_result blocks (user)   → separate {"role": "tool", tool_call_id, content} message
          - thinking blocks → no OpenAI equivalent; dropped

        The tool_use↔tool_result pairing invariant is preserved because the internal
        assistant→user message order is maintained through the conversion.
        """
        out: list[dict] = []
        if system_prompt:
            out.append({"role": "system", "content": system_prompt})

        for msg in messages:
            role = msg.get("role")
            blocks = msg.get("content")

            # Plain string content (uncommon) — pass through unchanged.
            if isinstance(blocks, str):
                out.append({"role": role or "user", "content": blocks})
                continue

            text_parts: list[str] = []
            tool_calls: list[dict] = []
            tool_msgs: list[dict] = []

            for b in blocks or []:
                btype = b.get("type")
                if btype == "text":
                    text_parts.append(b.get("text") or "")
                elif btype == "thinking":
                    continue  # no OpenAI equivalent; drop
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": b.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": b.get("name") or "",
                            "arguments": json.dumps(b.get("input") or {}),
                        },
                    })
                elif btype == "tool_result":
                    content = b.get("content")
                    tool_msgs.append({
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id") or "",
                        "content": content if isinstance(content, str) else json.dumps(content),
                    })

            if role == "assistant":
                assistant_msg: dict = {
                    "role": "assistant",
                    "content": "".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                # Skip entirely empty messages to avoid API rejection.
                if assistant_msg["content"] is not None or tool_calls:
                    out.append(assistant_msg)
            else:
                # user turn: text becomes a user message; tool results become tool messages.
                if text_parts:
                    out.append({"role": "user", "content": "".join(text_parts)})
                out.extend(tool_msgs)

        return out

    @staticmethod
    def _to_openai_tools(tools: list[dict]) -> list[dict]:
        """Convert internal (Anthropic-format) tool defs to OpenAI function tool format.

        Internal: {name, description, input_schema}
        OpenAI:   {type: "function", function: {name, description, parameters}}
        """
        out: list[dict] = []
        for tool in tools or []:
            out.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", "") or "",
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        return out

    # -- normalize ---------------------------------------------------------
    def normalize(self, native_response) -> AssistantResponse:
        """Normalize an OpenAI ChatCompletion response to `AssistantResponse`.

        Extracts text from `choices[0].message.content` → `TextBlock`,
        and tool calls from `choices[0].message.tool_calls` → `ToolUseBlock`
        (arguments are parsed from JSON string to dict).
        """
        content: list[ContentBlock] = []
        stop_reason = StopReason.END_TURN

        choices = getattr(native_response, "choices", None) or []
        if choices:
            choice = choices[0]
            stop_reason = self._map_stop_reason(getattr(choice, "finish_reason", None))
            message = getattr(choice, "message", None)
            if message is not None:
                text = getattr(message, "content", None)
                if text:
                    content.append(TextBlock(text=text))
                for tc in getattr(message, "tool_calls", None) or []:
                    fn = getattr(tc, "function", None)
                    content.append(ToolUseBlock(
                        id=getattr(tc, "id", "") or "",
                        name=getattr(fn, "name", "") or "",
                        input=self._parse_arguments(getattr(fn, "arguments", "")),
                    ))

        return AssistantResponse(
            content=content,
            stop_reason=stop_reason,
            usage=self._extract_usage(getattr(native_response, "usage", None)),
            id=getattr(native_response, "id", "") or "",
            model=getattr(native_response, "model", "") or "",
        )

    @staticmethod
    def _parse_arguments(raw) -> dict:
        """Parse OpenAI tool-call arguments to a dict.

        OpenAI returns `function.arguments` as a JSON string (unlike the
        Anthropic SDK, which pre-parses it). Returns `{}` on parse failure or
        empty input.
        """
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _map_stop_reason(raw) -> StopReason:
        """Map a finish_reason string to `StopReason`. Unknown values fall back to `END_TURN`."""
        if raw is None:
            return StopReason.END_TURN
        return _FINISH_REASON_MAP.get(str(raw), StopReason.END_TURN)

    @staticmethod
    def _extract_usage(usage) -> TokenUsage:
        """Map an OpenAI usage object to `TokenUsage`.

        Maps `prompt_tokens` → `input_tokens` and `completion_tokens` →
        `output_tokens`. Reads cached tokens from `prompt_tokens_details.cached_tokens`
        when available. OpenAI has no cache-creation concept, so that field is 0.
        """
        if usage is None:
            return TokenUsage()
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details is not None else 0
        return TokenUsage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cached or 0,
        )

    # -- exception mapping -------------------------------------------------
    def map_exception(self, exc: Exception) -> LLMError:
        """Map a native openai exception to the 5-class LLMError hierarchy.

          - context-length overflow → ContextOverflowError (propagated to caller for compaction)
          - 401 AuthenticationError → AuthError
          - 429 RateLimitError      → RateLimitError
          - connection / timeout / 5xx → TransientError
          - all other exceptions    → LLMError (base)
        """
        if isinstance(exc, LLMError):
            return exc

        if self._is_context_overflow(exc):
            return ContextOverflowError(str(exc))

        if isinstance(exc, openai.AuthenticationError):
            return AuthError(str(exc))

        if isinstance(exc, openai.RateLimitError):
            return RateLimitError(str(exc))

        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)):
            return TransientError(str(exc))
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status >= 500:
            return TransientError(str(exc))

        return LLMError(str(exc))

    @staticmethod
    def _is_context_overflow(exc: Exception) -> bool:
        """Return whether the exception represents a context-length overflow.

        OpenAI signals this as `BadRequestError` (HTTP 400) with
        `body.error.code == "context_length_exceeded"` or a matching phrase in
        the error message.
        """
        status = getattr(exc, "status_code", None)
        if status not in (400, 413):
            return False

        code = ""
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                code = str(err.get("code") or "")
        if code == "context_length_exceeded":
            return True

        haystack = " ".join([str(exc), str(getattr(exc, "message", "")), str(body)]).lower()
        return any(signal in haystack for signal in _CONTEXT_OVERFLOW_SIGNALS)
