from friday_agent.api.openai_provider import OpenAIProvider
from friday_agent.api.configs import OpenAIConfig


def _provider():
    return OpenAIProvider(api_key="test-key", model="gpt-4o")


def test_build_params_uses_default_model_and_max_tokens():
    p = _provider()
    params = p._build_params([], "sys", [], OpenAIConfig(max_tokens=512))
    assert params["model"] == "gpt-4o"
    assert params["max_tokens"] == 512
    assert params["stream"] is False
    # system_prompt is prepended as a system message.
    assert params["messages"][0] == {"role": "system", "content": "sys"}


def test_build_params_temperature():
    p = _provider()
    params = p._build_params([], "", [], OpenAIConfig(temperature=0.5))
    assert params["temperature"] == 0.5


def test_build_params_omits_empty_tools():
    p = _provider()
    params = p._build_params([], "", [], OpenAIConfig())
    assert "tools" not in params
