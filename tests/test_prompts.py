"""Tests for friday_agent/api/prompts.py — system prompt assembly (assemble_system_prompt)."""
from friday_agent.api.prompts import (
    GENERAL_AGENT_GUIDANCE,
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
    assert s.startswith("You are a research assistant.")
    assert GENERAL_AGENT_GUIDANCE in s


def test_assemble_empty_base_returns_guidance_only():
    assert str(assemble_system_prompt("")) == GENERAL_AGENT_GUIDANCE


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
