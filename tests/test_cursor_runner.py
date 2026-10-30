"""Translate real SDK dataclasses through the managed event protocol."""

import io
import json
from contextlib import contextmanager
from unittest.mock import MagicMock

from cursor_sdk import AgentOptions
from cursor_sdk.types import SDKAssistantMessage, SDKAssistantMessageContent, SDKToolUseMessage, TextBlock

from applypilot.apply import cursor_runner


def test_sdk_events_are_normalized_and_resources_closed(monkeypatch, tmp_path, capsys):
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": ["test"]}}}))
    transport = MagicMock()
    agent = transport.agents.create.return_value.__enter__.return_value
    agent.agent_id = "synthetic-agent-id"
    run = agent.send.return_value
    run.messages.return_value = [
        SDKToolUseMessage(
            type="tool_call", agent_id="test", run_id="run", call_id="call", name="browser_navigate", status="running"
        ),
        SDKAssistantMessage(
            type="assistant",
            agent_id="test",
            run_id="run",
            message=SDKAssistantMessageContent(content=[TextBlock(type="text", text="RESULT:PREVIEWED")]),
        ),
    ]
    result = run.wait.return_value
    result.status = "finished"
    result.result = "RESULT:PREVIEWED"
    result.usage = None
    result.error_message = None

    @contextmanager
    def session(workspace):
        yield transport

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cursor_runner, "cursor_session", session)
    monkeypatch.setattr(cursor_runner, "validate_model", lambda *args: None)
    monkeypatch.setattr(cursor_runner.signal, "signal", MagicMock())
    monkeypatch.setattr(cursor_runner.sys, "stdin", io.StringIO("synthetic prompt"))
    monkeypatch.setattr(
        cursor_runner.sys,
        "argv",
        ["cursor_runner", "--model", "test-model", "--mcp-config", str(mcp_path), "--dry-run"],
    )
    assert cursor_runner.main() == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["message"]["content"][0]["name"] == "browser_navigate"
    assert events[1]["delta"] is True
    assert events[-1]["result"] == "RESULT:PREVIEWED"
    assert events[-1]["task_id"] == "synthetic-agent-id"
    options = transport.agents.create.call_args.args[0]
    assert isinstance(options, AgentOptions)
    assert options.tools == ["mcp"]
    assert options.local.setting_sources == ["project"]
    transport.agents.create.return_value.__exit__.assert_called_once()
