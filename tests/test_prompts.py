"""Tests for friday_agent/api/prompts.py — system prompt assembly (assemble_system_prompt)."""
from friday_agent.api.prompts import (
    GENERAL_AGENT_GUIDANCE,
    TODO_GUIDANCE,
    assemble_system_prompt,
)


def test_assemble_returns_str_convertible():
    """assemble_system_prompt returns a SystemPrompt convertible to str."""
    result = assemble_system_prompt("PROMPT")
    assert isinstance(str(result), str)


def test_assemble_preserves_base_verbatim():
    """The base prompt is preserved verbatim in the output."""
    s = str(assemble_system_prompt("You are a helpful assistant."))
    assert "You are a helpful assistant." in s


def test_assemble_always_injects_general_guidance():
    s = str(assemble_system_prompt("You are a research assistant."))
    assert s.endswith("You are a research assistant.")
    assert GENERAL_AGENT_GUIDANCE in s


def test_assemble_empty_base_returns_general_then_todo_guidance():
    s = str(assemble_system_prompt(""))
    assert s == f"{GENERAL_AGENT_GUIDANCE}\n\n{TODO_GUIDANCE}"


def test_assemble_always_injects_todo_guidance():
    assert TODO_GUIDANCE in str(assemble_system_prompt("You are a research assistant."))
    assert TODO_GUIDANCE in str(assemble_system_prompt(""))


# --- Layer order: generic (SDK) -> specific (domain) -------------------------
# The caller's domain prompt comes last so its rules (e.g. an utterance policy)
# override the generic guidance by recency. With GENERAL last, its tone rules
# used to half-neutralize domain policies in production.

def test_general_guidance_precedes_base_prompt():
    out = str(assemble_system_prompt("DOMAIN-PROMPT"))
    assert out.index(GENERAL_AGENT_GUIDANCE) < out.index("DOMAIN-PROMPT")
    assert out.index(TODO_GUIDANCE) < out.index("DOMAIN-PROMPT")


def test_order_is_general_todo_base():
    out = str(assemble_system_prompt("DOMAIN-PROMPT"))
    assert out == f"{GENERAL_AGENT_GUIDANCE}\n\n{TODO_GUIDANCE}\n\nDOMAIN-PROMPT"


def test_colon_rule_does_not_mandate_preamble_text():
    # The colon clause also allows skipping text entirely, so the "Let me check
    # the file." example is not learned as a mandatory per-call preamble.
    assert "no accompanying text" in GENERAL_AGENT_GUIDANCE


def test_general_agent_guidance_is_domain_general_and_brand_free():
    lowered = GENERAL_AGENT_GUIDANCE.lower()
    # 개발 특화 섹션·브랜드 토큰이 섞이지 않아야 한다
    assert "# doing tasks" not in lowered
    assert "friday" not in lowered
    assert "claude" not in lowered
    assert "anthropic" not in lowered
    # 핵심 행동규칙이 포함돼야 한다
    assert "prompt injection" in lowered
    assert "# Executing actions with care" in GENERAL_AGENT_GUIDANCE
    assert "# Output efficiency" in GENERAL_AGENT_GUIDANCE
