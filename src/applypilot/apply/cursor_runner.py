"""Managed Cursor browser agent emitting the existing JSON event protocol."""

import argparse
import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path

from applypilot.apply.cursor_hook import install_hook
from applypilot.cursor_client import cursor_model_selection, cursor_session, validate_model
from applypilot.providers import redact


def emit(event: dict) -> None:
    """Write one event without leaking configured API keys."""
    print(redact(json.dumps(event)), flush=True)


def main() -> int:
    """Run one application with explicit client, agent, and cancellation ownership."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--mcp-config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    text = sys.stdin.read()
    workspace = Path.cwd()
    install_hook(workspace, args.dry_run)
    try:
        from cursor_sdk import AgentOptions, LocalAgentOptions

        servers = json.loads(args.mcp_config.read_text(encoding="utf-8"))["mcpServers"]
        with cursor_session(workspace) as client:
            validate_model(client, args.model)
            with client.agents.create(
                AgentOptions(
                    model=cursor_model_selection(args.model),
                    api_key=os.environ.get("CURSOR_API_KEY", ""),
                    tools=["mcp"],
                    local=LocalAgentOptions(cwd=workspace, setting_sources=["project"]),
                    mcp_servers=servers,
                )
            ) as agent:
                run = agent.send(text)

                def cancel(signum, frame):
                    run.cancel()

                signal.signal(signal.SIGTERM, cancel)
                signal.signal(signal.SIGINT, cancel)
                for message in run.messages():
                    if message.type == "assistant":
                        content = [asdict(block) for block in message.message.content if block.type == "text"]
                        emit({"type": "assistant", "delta": True, "message": {"content": content}})
                    elif message.type == "tool_call" and message.status == "running":
                        emit(
                            {
                                "type": "assistant",
                                "message": {"content": [{"type": "tool_use", "name": message.name, "input": {}}]},
                            }
                        )
                result = run.wait()
                emit(
                    {
                        "type": "result",
                        "result": result.result,
                        "is_error": result.status != "finished",
                        "usage": asdict(result.usage) if result.usage else {},
                        "error": result.error_message,
                        "task_id": agent.agent_id,
                    }
                )
                return 0 if result.status == "finished" else 1
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        emit({"type": "result", "result": "", "is_error": True, "error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
