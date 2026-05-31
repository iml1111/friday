"""Conversation summarization for caller-driven compaction."""
from __future__ import annotations

from friday_agent.api.provider import LLMProvider
from friday_agent.messages.types import (
    Message,
    create_user_message,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_OUTPUT_TOKENS_FOR_SUMMARY: int = 20_000

SUMMARIZER_SYSTEM_PROMPT: str = (
    "You are a conversation summarizer. Follow the instructions in the final message exactly."
)


# ---------------------------------------------------------------------------
# Compact prompt
# ---------------------------------------------------------------------------

COMPACT_PROMPT: str = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. Do NOT ask follow-up questions.
- You already have all the context you need in the conversation above.
- Tool calls will be rejected and will waste your only turn.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions. The summary must be thorough in capturing the key information, decisions, and context essential for continuing the work without losing context.

Before your final summary, wrap your analysis in <analysis> tags. In your analysis, go through the conversation chronologically and, for each part, identify: the user's explicit requests and intents; your approach to addressing them; key decisions and concepts; specific details (names, exact quotes, relevant excerpts, parameters); errors you ran into and how you fixed them; and any specific user feedback — especially where the user told you to do something differently. Then double-check for accuracy and completeness.

Your summary should include the following sections:
1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail.
2. Key Technical Concepts: List all important concepts, technologies, and techniques discussed.
3. Files and Code Sections: Enumerate specific files, resources, or artifacts examined, modified, or created. Include relevant excerpts and a note on why each matters.
4. Errors and fixes: List all errors you ran into and how you fixed them, including any user feedback.
5. Problem Solving: Document problems solved and any ongoing troubleshooting.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding feedback and changing intent.
7. Pending Tasks: Outline any pending tasks you have explicitly been asked to work on.
8. Current Work: Describe precisely what was being worked on immediately before this summary request.
9. Optional Next Step: List the next step that is directly in line with the user's most recent explicit request and the work in progress. Include direct quotes to avoid drift. Do not start tangential or already-completed work without confirming first.

Output format:
<analysis>your analysis (will be stripped)</analysis>
<summary>your summary here</summary>

REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block."""


# ---------------------------------------------------------------------------
# Summary message construction
# ---------------------------------------------------------------------------

_CONTINUATION_PREAMBLE: str = (
    "This session is being continued from a previous conversation that ran out of "
    "context. The summary below covers the earlier portion of the conversation.\n\n"
)
_RESUME_DIRECTIVE: str = (
    "\n\nContinue the conversation from where it left off without asking the user any "
    "further questions. Resume directly — do not acknowledge the summary, do not recap "
    "what was happening, do not preface with \"I'll continue\" or similar. Pick up the "
    "last task as if the break never happened."
)


def create_compact_summary_message(summary_text: str) -> Message:
    """Create a user message that carries the compaction summary.

    Wraps the extracted summary with continuation framing (so the model knows the
    session is resuming after a context cutoff) and a resume-directly directive
    (so it picks up the work without re-acknowledging the summary).

    Args:
        summary_text: Plain text extracted from the ``<summary>`` tag.

    Returns:
        A user Message with ``is_compact_summary=True``.
    """
    content = f"{_CONTINUATION_PREAMBLE}{summary_text}{_RESUME_DIRECTIVE}"
    return create_user_message(content=content, is_compact_summary=True)


# ---------------------------------------------------------------------------
# Compact conversation
# ---------------------------------------------------------------------------

async def compact_conversation(
    *,
    provider: LLMProvider,
    messages: list[dict],
) -> str:
    """Summarise a conversation and return the extracted summary text.

    Calls ``LLMProvider.complete()`` with ``tools=[]`` (no tool calls allowed
    during summarization), then extracts the text inside the ``<summary>`` tag.
    The ``<analysis>`` block is discarded. If no tags are present the entire
    response text is returned as a graceful fallback.

    Args:
        provider: LLM backend used to generate the summary.
        messages: Conversation history in API-ready ``list[dict]`` form.

    Returns:
        Extracted summary text (stripped of surrounding whitespace).
    """
    compact_messages = list(messages) + [{"role": "user", "content": COMPACT_PROMPT}]

    config = provider.config_type(max_tokens=MAX_OUTPUT_TOKENS_FOR_SUMMARY)

    response = await provider.complete(
        messages=compact_messages,
        system_prompt=SUMMARIZER_SYSTEM_PROMPT,
        tools=[],  # No tool calls permitted during summarization.
        config=config,
    )

    raw_text = ""
    for block in response.content:
        if hasattr(block, "text") and block.text:
            raw_text += block.text

    if "<summary>" in raw_text and "</summary>" in raw_text:
        start = raw_text.index("<summary>") + len("<summary>")
        end = raw_text.index("</summary>")
        return raw_text[start:end].strip()

    # No structured tags — return the full response as a best-effort fallback.
    return raw_text.strip()
