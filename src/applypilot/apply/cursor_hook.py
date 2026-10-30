"""Cursor MCP policy: preserve Gmail restrictions and prevent preview email sends."""

import json
import sys
from pathlib import Path

GMAIL_READ_TOOLS = {"search_emails", "read_email"}


def allowed(payload: dict, dry_run: bool) -> bool:
    """Limit Gmail to verification reads, plus submission emails when enabled."""
    server = payload.get("mcp_server_name", "")
    name = payload.get("tool_name", "")
    if server == "gmail" or "gmail" in name.lower():
        name = name.rsplit("__", 1)[-1]
        return name in GMAIL_READ_TOOLS or (name == "send_email" and not dry_run)
    return server == "playwright"


def install_hook(workspace: Path, dry_run: bool) -> None:
    """Install the packaged policy in a worker's isolated project settings."""
    directory = workspace / ".cursor"
    directory.mkdir(parents=True, exist_ok=True)
    policy = Path(__file__).resolve()
    import shlex

    command = shlex.join([sys.executable, str(policy), "preview" if dry_run else "submit"])
    (directory / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {"beforeMCPExecution": [{"command": command, "failClosed": True}]},
            }
        ),
        encoding="utf-8",
    )


def main() -> None:
    """Read Cursor hook JSON and fail closed on malformed requests."""
    try:
        payload = json.load(sys.stdin)
        permitted = allowed(payload, len(sys.argv) < 2 or sys.argv[1] != "submit")
    except (ValueError, TypeError, AttributeError):
        permitted = False
    print(json.dumps({"permission": "allow" if permitted else "deny"}))
    if not permitted:
        sys.exit(2)


if __name__ == "__main__":
    main()
