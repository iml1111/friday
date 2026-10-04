"""Conversation summarization for caller-driven compaction."""
from __future__ import annotations

import copy

from friday_agent.api.provider import AssistantResponse, LLMConfig, LLMProvider, ToolUseBlock
from friday_agent.messages.types import (
    Message,
    create_user_message,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_OUTPUT_TOKENS_FOR_SUMMARY: int = 20_000


# ---------------------------------------------------------------------------
# Compact prompt
# ---------------------------------------------------------------------------
# Split into head (guards + section spec) and tail (output format reminder) so
# callers can inject domain requirements between them. The tail must stay last:
# it carries the <analysis>/<summary> output contract, and recency is what makes
# the model honor it. Format compliance is load-bearing — a response without the
# tags falls back to raw-text extraction (see compact_conversation), which silently
# leaks the scratchpad into the summary.
#
# The no-tools guard appears ONCE, in the CRITICAL line. engine.compact() sends
# the agent's own tools (to share step()'s cached prefix), so the CRITICAL line is
# the request; a tool_use reply is retried once with tools=[], where both adapters
# omit the field and the model cannot emit a tool_use at all (see
# compact_conversation).

_COMPACT_PROMPT_HEAD: str = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. Do NOT ask follow-up questions.
- You already have all the context you need in the conversation above.
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
9. Optional Next Step: List the next step that is directly in line with the user's most recent explicit request and the work in progress. Include direct quotes to avoid drift. Do not start tangential or already-completed work without confirming first."""

_COMPACT_PROMPT_TAIL: str = """Output format:
<analysis>your analysis (will be stripped)</analysis>
<summary>your summary here</summary>

REMINDER: Respond with plain text only — an <analysis> block followed by a <summary> block."""

_EXTRA_INSTRUCTIONS_HEADER: str = (
    "Domain-specific requirements for this summary (these take precedence over the "
    "generic sections above):"
)


def build_compact_prompt(extra_instructions: str = "") -> str:
    """Render the compaction prompt, optionally with caller-supplied domain rules.

    The extra block lands after the nine generic sections and before the output
    format reminder. Its header grants it precedence, so callers can
    both add requirements ("also capture the candidate shortlist") and redefine
    generic ones ("for section 3, list resource IDs instead of code excerpts")
    without forking the base prompt.

    Args:
        extra_instructions: Domain summary requirements. Blank (or whitespace)
            renders the base prompt unchanged, byte for byte.

    Returns:
        The full prompt text to send as the final user message.
    """
    extra = extra_instructions.strip()
    middle = [f"{_EXTRA_INSTRUCTIONS_HEADER}\n{extra}"] if extra else []
    return "\n\n".join([_COMPACT_PROMPT_HEAD, *middle, _COMPACT_PROMPT_TAIL])


COMPACT_PROMPT: str = build_compact_prompt()


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
    system_prompt: str,
    tools: list[dict],
    config: LLMConfig,
    extra_instructions: str = "",
) -> str:
    """Summarise a conversation and return the extracted summary text.

    The call runs under the caller's system_prompt, tools and config (max_tokens
    raised to at least MAX_OUTPUT_TOKENS_FOR_SUMMARY) — the agent passes step()'s
    own, so the provider can serve the history from cache. If a reply sent with
    tools contains a tool_use or no usable <summary>, the call is retried once
    with tools=[] (same system prompt and config). The <analysis> block is
    discarded; without a usable <summary> pair (see _extract_summary) the entire
    response text is returned as a graceful fallback.

    Args:
        provider: LLM backend used to generate the summary.
        messages: Conversation history in API-ready ``list[dict]`` form.
        system_prompt: System prompt of the summary call.
        tools: Tool schemas of the summary call ([] = no tools).
        config: Call config of the summary call; copied, never mutated.
        extra_instructions: Domain summary requirements folded into the compact
            prompt (see ``build_compact_prompt``). Blank means the base prompt.

    Returns:
        Extracted summary text (stripped of surrounding whitespace).
    """
    prompt = build_compact_prompt(extra_instructions)
    compact_messages = list(messages) + [{"role": "user", "content": prompt}]

    # Same config as the agent's turns (the message cache keys on settings such
    # as thinking); only the output budget is raised for the summary.
    summary_config = copy.copy(config)
    summary_config.max_tokens = max(config.max_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)

    response = await provider.complete(
        messages=compact_messages,
        system_prompt=system_prompt,
        tools=tools,
        config=summary_config,
    )
    if tools and (_has_tool_use(response) or _extract_summary(_response_text(response)) is None):
        # The shared prefix exposes the agent's tools: a tool call (or a reply with
        # no summary) is retried once without them — the only call that pays the
        # full price for the history. A tool_use never enters state either way.
        response = await provider.complete(
            messages=compact_messages,
            system_prompt=system_prompt,
            tools=[],
            config=summary_config,
        )

    raw_text = _response_text(response)
    summary = _extract_summary(raw_text)
    if summary is not None:
        return summary

    # No usable <summary> pair — return the full response as a best-effort fallback.
    return raw_text.strip()


def _response_text(response: AssistantResponse) -> str:
    """Concatenate the response's text blocks."""
    return "".join(block.text for block in response.content if getattr(block, "text", None))


def _has_tool_use(response: AssistantResponse) -> bool:
    return any(isinstance(block, ToolUseBlock) for block in response.content)


def _extract_summary(raw_text: str) -> str | None:
    """Return the text inside ``<summary>``, or None if there is no usable pair.

    The closing tag is searched for only *after* the opening tag: a summarizer
    that closes its ``<analysis>`` block with ``</summary>`` by mistake puts a
    closing tag ahead of the real opening one, and pairing the first of each
    yields an empty summary. An empty body is reported as a miss as well, so the
    caller falls back instead of replacing the history with nothing.
    """
    start = raw_text.find("<summary>")
    if start == -1:
        return None
    start += len("<summary>")
    end = raw_text.find("</summary>", start)
    if end == -1:
        return None
    return raw_text[start:end].strip() or None
