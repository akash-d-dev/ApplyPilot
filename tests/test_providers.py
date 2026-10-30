"""Provider selection, credentials, and SDK compatibility."""

import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dotenv import dotenv_values
from typer.testing import CliRunner

from applypilot import config, llm
from applypilot.cli import app
from applypilot.cursor_client import CursorLLMClient, cursor_session
from applypilot.providers import agent_model, agent_requirements, apply_agent, llm_provider


def test_cursor_only_key_selects_full_pipeline(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-placeholder")
    assert llm_provider() == "cursor"
    assert apply_agent() == "cursor"
    assert isinstance(llm.get_client(), CursorLLMClient)
    assert config.get_tier() == 3


def test_existing_provider_precedence_and_explicit_cursor(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-placeholder")
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    assert llm_provider() == "gemini"
    assert apply_agent() == "claude"
    monkeypatch.setenv("LLM_URL", "http://localhost:1234/v1")
    assert llm_provider() == "local"
    monkeypatch.setenv("LLM_PROVIDER", "cursor")
    assert llm_provider() == "cursor"
    monkeypatch.setenv("APPLY_AGENT", "claude")
    assert apply_agent() == "claude"


@pytest.mark.parametrize("provider", ["cursor", "gemini", "openai", "local", "unknown"])
def test_invalid_or_missing_credentials_fail_explicitly(monkeypatch, provider):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    with pytest.raises(RuntimeError):
        llm_provider()


def test_model_overrides_are_independent(monkeypatch):
    monkeypatch.setenv("CURSOR_MODEL", "browser-model")
    monkeypatch.setenv("LLM_MODEL", "text-model")
    assert agent_model("cursor") == "browser-model"
    assert agent_model("cursor", "explicit-model") == "explicit-model"
    assert agent_model("claude") == "haiku"
    assert CursorLLMClient().model == "text-model"


def test_missing_sdk_actionable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "cursor_sdk", None)
    with pytest.raises(RuntimeError, match="pip install"), cursor_session(tmp_path):
        pass
    assert any("CURSOR_API_KEY" in item for item in agent_requirements("cursor"))


def test_cursor_requests_use_fresh_tool_free_agents(monkeypatch):
    import applypilot.cursor_client as module

    clients = []

    @contextmanager
    def session(workspace):
        client = MagicMock()
        client.models.list.return_value = [SimpleNamespace(id="test-model")]
        result = SimpleNamespace(status="finished", result='{"score": 9}', error_message=None)
        client.agents.create.return_value.__enter__.return_value.send.return_value.wait.return_value = result
        clients.append(client)
        yield client

    monkeypatch.setattr(module, "cursor_session", session)
    client = CursorLLMClient("test-model")
    messages = [{"role": "system", "content": "JSON only"}, {"role": "user", "content": "Score"}]
    assert client.chat(messages, max_tokens=512, temperature=0.2) == '{"score": 9}'
    assert client.ask("Another independent request") == '{"score": 9}'
    assert len(clients) == 3
    for transport in clients[1:]:
        options = transport.agents.create.call_args.args[0]
        assert options.tools == []
        assert options.local.setting_sources == []
        transport.agents.create.return_value.__exit__.assert_called_once()
    assert messages[0]["content"] == "JSON only"


def test_model_rejection_before_agent_creation(monkeypatch):
    import applypilot.cursor_client as module

    transport = MagicMock()
    transport.models.list.return_value = []

    @contextmanager
    def session(workspace):
        yield transport

    monkeypatch.setattr(module, "cursor_session", session)
    with pytest.raises(RuntimeError, match="unavailable"):
        CursorLLMClient("missing-model").ask("test")
    transport.agents.create.assert_not_called()


def test_authentication_error_redacted(monkeypatch, tmp_path):
    from cursor_sdk import CursorClient
    from cursor_sdk.errors import AuthenticationError

    secret = "synthetic-credential-for-test"
    monkeypatch.setenv("CURSOR_API_KEY", secret)
    monkeypatch.setattr(CursorClient, "launch_bridge", MagicMock(side_effect=AuthenticationError(f"Rejected {secret}")))
    with pytest.raises(RuntimeError) as captured, cursor_session(tmp_path):
        pass
    assert secret not in str(captured.value)
    assert "[redacted]" in str(captured.value)


def test_hidden_credential_setup_preserves_settings(monkeypatch):
    import getpass

    import applypilot.wizard.init as wizard

    monkeypatch.setattr(wizard, "ENV_PATH", config.ENV_PATH)
    password_input = MagicMock(return_value="synthetic-placeholder")
    monkeypatch.setattr(getpass, "getpass", password_input)
    config.ENV_PATH.write_text("CAPSOLVER_API_KEY=existing-placeholder\n")
    response = CliRunner().invoke(app, ["configure-cursor"], input="test-model\n")
    assert response.exit_code == 0, response.output
    assert "synthetic-placeholder" not in response.output
    password_input.assert_called_once()
    values = dotenv_values(config.ENV_PATH)
    assert values["CURSOR_API_KEY"] == "synthetic-placeholder"
    assert values["CAPSOLVER_API_KEY"] == "existing-placeholder"
    assert values["LLM_PROVIDER"] == values["APPLY_AGENT"] == "cursor"
    assert config.ENV_PATH.stat().st_mode & 0o777 == 0o600
