"""Tests for compaction — the prompt and summary message (context/compact.py) and engine.compact()'s summary extraction."""
import pytest

from friday_agent.api.provider import (
    AssistantResponse,
    StopReason,
    TextBlock,
    TokenUsage,
)
from friday_agent.context.compact import (
    COMPACT_PROMPT,
    build_compact_prompt,
    create_compact_summary_message,
)
from friday_agent.core.engine import FridayAgent
from friday_agent.core.state import LoopState
from friday_agent.messages.types import create_user_message
from tests.fakes import FakeLLMProvider


async def _compact(provider: FakeLLMProvider) -> str:
    """Run engine.compact() over a one-message state; return the summary message text."""
    engine = FridayAgent(provider=provider, tools=[])
    state = await engine.compact(LoopState(messages=[create_user_message("x")]))
    return state.messages[0].content[0].text


def _summary_response(text: str = "<summary>s</summary>") -> AssistantResponse:
    return AssistantResponse(
        content=[TextBlock(type="text", text=text)],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


# ---------------------------------------------------------------------------
# engine.compact(): <summary> extraction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_extracts_summary_tag():
    """The <analysis> block is discarded; only the <summary> body is kept."""
    provider = FakeLLMProvider(responses=[_summary_response("<analysis>scratch</analysis><summary>COMPACTED</summary>")])

    text = await _compact(provider)

    assert "COMPACTED" in text
    assert "scratch" not in text
    assert provider.call_count == 1


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
    # Domain-agnostic: dev-only phrasing must not creep back in
    assert "function signatures" not in COMPACT_PROMPT


def test_no_tools_guard_appears_exactly_once():
    """One no-tools request is enough: a reply that calls a tool anyway is retried
    with tools=[], where both adapters drop the field and tool_use is impossible.
    Repeating the guard is dead weight."""
    assert COMPACT_PROMPT.count("Do NOT call any tools") == 1
    # The trailing reminder guards the OUTPUT FORMAT, not tool use.
    assert COMPACT_PROMPT.rstrip().endswith(
        "REMINDER: Respond with plain text only — an <analysis> block followed by a <summary> block."
    )


def test_create_compact_summary_message_has_continuation_framing():
    """The summary message wraps the summary with continuation + resume-directly framing."""
    msg = create_compact_summary_message("THE-SUMMARY-BODY")
    text = " ".join(b.text or "" for b in msg.content if b.text)
    assert msg.is_compact_summary is True
    assert "THE-SUMMARY-BODY" in text
    assert "continued from a previous conversation" in text
    assert "without asking the user any further questions" in text


# ---------------------------------------------------------------------------
# build_compact_prompt: optional domain-instruction slot
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_build_compact_prompt_blank_is_byte_identical_to_base(blank):
    """No injection (or whitespace only) must render the base prompt unchanged."""
    assert build_compact_prompt(blank) == COMPACT_PROMPT


def test_build_compact_prompt_places_extra_between_sections_and_output_format():
    """The domain block lands after section 9 and before the output format + REMINDER.

    Order matters: the trailing REMINDER carries the <analysis>/<summary> output
    contract, and recency is what makes the model honor it. A response without the
    tags degrades to raw-text extraction, so an injected block must never displace
    the reminder from the end of the prompt.
    """
    prompt = build_compact_prompt("Always preserve the candidate shortlist.")

    section_9 = prompt.index("9. Optional Next Step")
    extra = prompt.index("Always preserve the candidate shortlist.")
    output_format = prompt.index("Output format:")
    reminder = prompt.index("REMINDER: Respond with plain text only")

    assert section_9 < extra < output_format < reminder
    assert prompt.rstrip().endswith("followed by a <summary> block.")
    # The header grants the block precedence over the generic sections.
    assert "take precedence over the generic sections above" in prompt


def test_build_compact_prompt_keeps_base_prompt_intact():
    """Injection is additive — every base guarantee survives."""
    prompt = build_compact_prompt("domain rules")
    assert COMPACT_PROMPT != prompt
    for n in range(1, 10):
        assert f"{n}." in prompt
    assert "Do NOT call any tools" in prompt
    assert "<analysis>" in prompt and "<summary>" in prompt


# ---------------------------------------------------------------------------
# <summary> extraction: the closing tag is searched only after the opening tag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compact_ignores_stray_closing_tag_before_summary():
    """An <analysis> block closed with </summary> must not empty the summary."""
    raw = "<analysis>notes</summary>\n<summary>REAL BODY</summary>"
    provider = FakeLLMProvider(responses=[_summary_response(raw)])

    text = await _compact(provider)

    assert "REAL BODY" in text
    assert "notes" not in text


@pytest.mark.asyncio
async def test_compact_empty_summary_falls_back_to_full_text():
    """An empty body is a miss: one no-tools retry, then the full text."""
    raw = "<analysis>notes</analysis><summary>  </summary>"
    provider = FakeLLMProvider(responses=[_summary_response(raw), _summary_response(raw)])

    text = await _compact(provider)

    assert raw in text
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_compact_unclosed_summary_falls_back_to_full_text():
    raw = "<analysis>notes</analysis><summary>cut off mid-sentence"
    provider = FakeLLMProvider(responses=[_summary_response(raw), _summary_response(raw)])

    text = await _compact(provider)

    assert raw in text
