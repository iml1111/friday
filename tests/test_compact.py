"""Tests for friday_agent/context/compact.py — conversation compaction."""
import pytest

from friday_agent.api.provider import (
    AssistantResponse,
    StopReason,
    TextBlock,
    TokenUsage,
)
from friday_agent.context.compact import (
    COMPACT_PROMPT,
    SUMMARIZER_SYSTEM_PROMPT,
    compact_conversation,
    create_compact_summary_message,
)
from tests.fakes import FakeLLMProvider


# ---------------------------------------------------------------------------
# compact_conversation: produces summary via LLMProvider.complete()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_conversation_extracts_summary_tag():
    """compact_conversation calls provider.complete() and extracts the <summary> tag."""
    summary_text = "COMPACTED"
    response = AssistantResponse(
        content=[TextBlock(type="text", text=f"<analysis>scratch</analysis><summary>{summary_text}</summary>")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )
    provider = FakeLLMProvider(responses=[response])

    messages = [{"role": "user", "content": "hello"}]
    result = await compact_conversation(provider=provider, messages=messages)

    assert result == summary_text
    # provider.complete() must have been called exactly once
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_compact_conversation_uses_no_tools():
    """compact_conversation must call complete() with an empty tools list."""
    response = AssistantResponse(
        content=[TextBlock(type="text", text="<summary>short</summary>")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )
    provider = FakeLLMProvider(responses=[response])

    await compact_conversation(provider=provider, messages=[{"role": "user", "content": "x"}])

    assert provider.received_tools[0] == [], "compact must send empty tools list"


@pytest.mark.asyncio
async def test_compact_conversation_no_summary_tag_returns_raw():
    """If the response has no <summary> tag, return the full text (graceful fallback)."""
    raw_text = "No tags here, just plain text."
    response = AssistantResponse(
        content=[TextBlock(type="text", text=raw_text)],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )
    provider = FakeLLMProvider(responses=[response])

    result = await compact_conversation(provider=provider, messages=[{"role": "user", "content": "x"}])
    assert result == raw_text


def test_compact_prompt_has_enriched_structure():
    """Enriched COMPACT_PROMPT carries strong no-tools framing, analysis instruction,
    all 9 sections, and the <analysis>/<summary> format — generalized (no dev-only phrasing)."""
    assert "Do NOT call any tools" in COMPACT_PROMPT
    assert "<analysis>" in COMPACT_PROMPT and "<summary>" in COMPACT_PROMPT
    for n in range(1, 10):
        assert f"{n}." in COMPACT_PROMPT, f"section {n} missing"
    assert "Primary Request and Intent" in COMPACT_PROMPT
    assert "All user messages" in COMPACT_PROMPT
    assert "Optional Next Step" in COMPACT_PROMPT
    # 도메인 무관: 개발 전용 표현이 재유입되지 않아야 한다
    assert "function signatures" not in COMPACT_PROMPT


def test_create_compact_summary_message_has_continuation_framing():
    """The summary message wraps the summary with continuation + resume-directly framing."""
    msg = create_compact_summary_message("THE-SUMMARY-BODY")
    text = " ".join(b.text or "" for b in msg.content if b.text)
    assert msg.is_compact_summary is True
    assert "THE-SUMMARY-BODY" in text
    assert "continued from a previous conversation" in text
    assert "without asking the user any further questions" in text


@pytest.mark.asyncio
async def test_compact_conversation_uses_dedicated_summarizer_system():
    """With no system_prompt override, the summary call uses SUMMARIZER_SYSTEM_PROMPT."""
    response = AssistantResponse(
        content=[TextBlock(type="text", text="<summary>s</summary>")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )
    provider = FakeLLMProvider(responses=[response])
    await compact_conversation(provider=provider, messages=[{"role": "user", "content": "x"}])
    assert provider.received_system_prompts[0] == SUMMARIZER_SYSTEM_PROMPT
