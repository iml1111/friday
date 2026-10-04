"""Tests for compaction — the prompt texts (api/prompts.py) and what engine.compact() sends and builds."""
import pytest

from friday_agent.api.prompts import format_compact_prompt
from friday_agent.api.provider import (
    AssistantResponse,
    LLMError,
    StopReason,
    TextBlock,
    ThinkingBlock,
    TokenUsage,
)
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.messages.types import Message, create_user_message
from tests.fakes import FakeLLMProvider

BASE_PROMPT = format_compact_prompt()


def _summary_response(text: str = "<summary>s</summary>") -> AssistantResponse:
    return AssistantResponse(
        content=[TextBlock(type="text", text=text)],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


async def _compact(provider: FakeLLMProvider, compact_instructions: str = "") -> Message:
    """Run engine.compact() over a one-message state; return the summary message."""
    engine = FridayAgent(provider=provider, tools=[], compact_instructions=compact_instructions)
    state = await engine.compact(LoopState(messages=[create_user_message("x")]))
    return state.messages[0]


async def _sent_prompt(compact_instructions: str) -> str:
    provider = FakeLLMProvider(responses=[_summary_response()])
    await _compact(provider, compact_instructions)
    return provider.received_messages[0][-1]["content"]


# ---------------------------------------------------------------------------
# Compaction prompt text
# ---------------------------------------------------------------------------

def test_compact_prompt_has_enriched_structure():
    """The compaction prompt carries strong no-tools framing, analysis instruction,
    all 9 sections, and the <analysis>/<summary> format — generalized (no dev-only phrasing)."""
    assert "Do NOT call any tools" in BASE_PROMPT
    assert "<analysis>" in BASE_PROMPT and "<summary>" in BASE_PROMPT
    for n in range(1, 10):
        assert f"{n}." in BASE_PROMPT, f"section {n} missing"
    assert "Primary Request and Intent" in BASE_PROMPT
    assert "All user messages" in BASE_PROMPT
    assert "Optional Next Step" in BASE_PROMPT
    # Domain-agnostic: dev-only phrasing must not creep back in
    assert "function signatures" not in BASE_PROMPT


def test_no_tools_guard_appears_exactly_once():
    """One no-tools request is enough: a reply that calls a tool anyway is retried
    with tools=[], where both adapters drop the field and tool_use is impossible.
    Repeating the guard is dead weight."""
    assert BASE_PROMPT.count("Do NOT call any tools") == 1
    # The trailing reminder guards the OUTPUT FORMAT, not tool use.
    assert BASE_PROMPT.rstrip().endswith(
        "REMINDER: Respond with plain text only — an <analysis> block followed by a <summary> block."
    )


# ---------------------------------------------------------------------------
# engine.compact(): compact_instructions slot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
async def test_blank_instructions_send_the_base_prompt(blank):
    """No injection (or whitespace only) sends the base prompt unchanged."""
    assert await _sent_prompt(blank) == BASE_PROMPT


@pytest.mark.asyncio
async def test_instructions_land_between_sections_and_output_format():
    """The domain block lands after section 9 and before the output format + REMINDER.

    Order matters: the trailing REMINDER carries the <analysis>/<summary> output
    contract, and recency is what makes the model honor it. A response without the
    tags degrades to raw-text extraction, so an injected block must never displace
    the reminder from the end of the prompt.
    """
    prompt = await _sent_prompt("Always preserve the candidate shortlist.")

    section_9 = prompt.index("9. Optional Next Step")
    extra = prompt.index("Always preserve the candidate shortlist.")
    output_format = prompt.index("Output format:")
    reminder = prompt.index("REMINDER: Respond with plain text only")

    assert section_9 < extra < output_format < reminder
    assert prompt.rstrip().endswith("followed by a <summary> block.")
    # The header grants the block precedence over the generic sections.
    assert "take precedence over the generic sections above" in prompt


@pytest.mark.asyncio
async def test_instructions_keep_the_base_prompt_intact():
    """Injection is additive — every base guarantee survives."""
    prompt = await _sent_prompt("domain rules")
    assert prompt != BASE_PROMPT
    for n in range(1, 10):
        assert f"{n}." in prompt
    assert "Do NOT call any tools" in prompt
    assert "<analysis>" in prompt and "<summary>" in prompt


# ---------------------------------------------------------------------------
# engine.compact(): summary message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_message_has_continuation_framing():
    """The summary message wraps the summary with continuation + resume-directly framing."""
    msg = await _compact(FakeLLMProvider(responses=[_summary_response("<summary>THE-SUMMARY-BODY</summary>")]))
    text = msg.content[0].text
    assert msg.type == "user"
    assert msg.is_compact_summary is True
    assert "THE-SUMMARY-BODY" in text
    assert "continued from a previous conversation" in text
    assert "without asking the user any further questions" in text


# ---------------------------------------------------------------------------
# engine.compact(): <summary> extraction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_extracts_summary_tag():
    """The <analysis> block is discarded; only the <summary> body is kept."""
    provider = FakeLLMProvider(responses=[_summary_response("<analysis>scratch</analysis><summary>COMPACTED</summary>")])

    text = (await _compact(provider)).content[0].text

    assert "COMPACTED" in text
    assert "scratch" not in text
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_compact_ignores_stray_closing_tag_before_summary():
    """An <analysis> block closed with </summary> must not empty the summary."""
    raw = "<analysis>notes</summary>\n<summary>REAL BODY</summary>"
    provider = FakeLLMProvider(responses=[_summary_response(raw)])

    text = (await _compact(provider)).content[0].text

    assert "REAL BODY" in text
    assert "notes" not in text


@pytest.mark.asyncio
async def test_compact_empty_summary_falls_back_to_full_text():
    """An empty body is a miss: one no-tools retry, then the full text."""
    raw = "<analysis>notes</analysis><summary>  </summary>"
    provider = FakeLLMProvider(responses=[_summary_response(raw), _summary_response(raw)])

    text = (await _compact(provider)).content[0].text

    assert raw in text
    assert provider.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [[], [TextBlock(text="  \n")], [ThinkingBlock(thinking="planning the summary")]],
    ids=["empty", "whitespace", "thinking-only"],
)
async def test_compact_raises_when_the_reply_has_no_text(content):
    """A reply with no text, even after the no-tools retry, must not replace the
    history with an empty summary: compact() raises so the caller keeps its state."""
    reply = AssistantResponse(content=content, stop_reason=StopReason.END_TURN, usage=TokenUsage())
    provider = FakeLLMProvider(responses=[reply, reply])

    with pytest.raises(LLMError, match="no text"):
        await _compact(provider)
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_compact_unclosed_summary_falls_back_to_full_text():
    raw = "<analysis>notes</analysis><summary>cut off mid-sentence"
    provider = FakeLLMProvider(responses=[_summary_response(raw), _summary_response(raw)])

    text = (await _compact(provider)).content[0].text

    assert raw in text
