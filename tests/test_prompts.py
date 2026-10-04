"""Tests for friday_agent/api/prompts.py — system prompt assembly (assemble_system_prompt)."""
from friday_agent.api.prompts import (
    GENERAL_AGENT_GUIDANCE,
    TODO_GUIDANCE,
    assemble_system_prompt,
    format_todo_reminder,
)
from friday_agent.messages.types import SYSTEM_REMINDER_PREFIX


def test_assemble_returns_plain_str():
    assert isinstance(assemble_system_prompt("PROMPT"), str)


def test_assemble_preserves_base_verbatim():
    """The base prompt is preserved verbatim in the output."""
    s = assemble_system_prompt("You are a helpful assistant.")
    assert "You are a helpful assistant." in s


def test_assemble_always_injects_general_guidance():
    s = assemble_system_prompt("You are a research assistant.")
    assert s.endswith("You are a research assistant.")
    assert GENERAL_AGENT_GUIDANCE in s


def test_assemble_empty_base_returns_general_then_todo_guidance():
    s = assemble_system_prompt("")
    assert s == f"{GENERAL_AGENT_GUIDANCE}\n\n{TODO_GUIDANCE}"


def test_assemble_always_injects_todo_guidance():
    assert TODO_GUIDANCE in assemble_system_prompt("You are a research assistant.")
    assert TODO_GUIDANCE in assemble_system_prompt("")


# --- Layer order: generic (SDK) -> specific (domain) -------------------------
# The caller's domain prompt comes last so its rules (e.g. an utterance policy)
# override the generic guidance by recency. With GENERAL last, its tone rules
# used to half-neutralize domain policies in production.

def test_general_guidance_precedes_base_prompt():
    out = assemble_system_prompt("DOMAIN-PROMPT")
    assert out.index(GENERAL_AGENT_GUIDANCE) < out.index("DOMAIN-PROMPT")
    assert out.index(TODO_GUIDANCE) < out.index("DOMAIN-PROMPT")


def test_order_is_general_todo_base():
    out = assemble_system_prompt("DOMAIN-PROMPT")
    assert out == f"{GENERAL_AGENT_GUIDANCE}\n\n{TODO_GUIDANCE}\n\nDOMAIN-PROMPT"


def test_memory_instructions_sit_between_guidance_and_base():
    out = assemble_system_prompt("DOMAIN-PROMPT", "MEMORY-INSTRUCTIONS")
    assert out == f"{GENERAL_AGENT_GUIDANCE}\n\n{TODO_GUIDANCE}\n\nMEMORY-INSTRUCTIONS\n\nDOMAIN-PROMPT"


def test_memory_instructions_without_base():
    out = assemble_system_prompt("", "MEMORY-INSTRUCTIONS")
    assert out == f"{GENERAL_AGENT_GUIDANCE}\n\n{TODO_GUIDANCE}\n\nMEMORY-INSTRUCTIONS"


# --- Per-turn todo reminder ---------------------------------------------------

def test_todo_reminder_empty_is_blank():
    assert format_todo_reminder([]) == ""


def test_todo_reminder_formats_items():
    out = format_todo_reminder([
        {"content": "Wire it up", "status": "in_progress"},
        {"content": "Add tests", "status": "pending"},
    ])
    assert "<system-reminder>" in out and "</system-reminder>" in out
    assert "- [in_progress] Wire it up" in out
    assert "- [pending] Add tests" in out


def test_todo_reminder_starts_with_shared_detection_prefix():
    # Must open with the exact constant the Anthropic adapter's breakpoint
    # skip matches on (see messages/types.py SYSTEM_REMINDER_PREFIX).
    out = format_todo_reminder([{"content": "task", "status": "pending"}])
    assert out.startswith(SYSTEM_REMINDER_PREFIX)


def test_colon_rule_does_not_mandate_preamble_text():
    # The colon clause also allows skipping text entirely, so the "Let me check
    # the file." example is not learned as a mandatory per-call preamble.
    assert "no accompanying text" in GENERAL_AGENT_GUIDANCE


def test_general_agent_guidance_is_domain_general_and_brand_free():
    lowered = GENERAL_AGENT_GUIDANCE.lower()
    # No dev-specific sections or brand tokens may leak in
    assert "# doing tasks" not in lowered
    assert "friday" not in lowered
    assert "claude" not in lowered
    assert "anthropic" not in lowered
    # Core behavioral rules must be present
    assert "prompt injection" in lowered
    assert "# Executing actions with care" in GENERAL_AGENT_GUIDANCE
    assert "# Output efficiency" in GENERAL_AGENT_GUIDANCE
