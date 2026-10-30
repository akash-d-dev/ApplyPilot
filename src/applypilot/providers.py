"""Provider selection shared by the CLI, feature gates, and AI clients."""

import importlib.util
import os
import shutil

CURSOR_DEFAULT_MODEL = "composer-2.5"


def llm_provider() -> str:
    """Resolve an explicit provider or preserve the existing detection order."""
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()
    keys = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "local": "LLM_URL", "cursor": "CURSOR_API_KEY"}
    if explicit:
        if explicit not in keys:
            raise RuntimeError("LLM_PROVIDER must be gemini, openai, local, or cursor.")
        if not os.environ.get(keys[explicit]):
            raise RuntimeError(f"{explicit} requires {keys[explicit]}.")
        return explicit
    if os.environ.get("LLM_URL"):
        return "local"
    for provider in ("gemini", "openai", "cursor"):
        if os.environ.get(keys[provider]):
            return provider
    raise RuntimeError("No LLM provider configured. Set CURSOR_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL.")


def apply_agent(override: str | None = None) -> str:
    """Select the browser agent without silently replacing an existing setup."""
    selected = override or os.environ.get("APPLY_AGENT", "")
    if not selected:
        selected = "cursor" if llm_provider() == "cursor" else "claude"
    selected = selected.strip().lower()
    if selected not in ("cursor", "claude"):
        raise RuntimeError("APPLY_AGENT / --agent must be cursor or claude.")
    return selected


def agent_model(agent: str, override: str | None = None) -> str:
    """Resolve the browser model independently from text-stage overrides."""
    if override:
        return override
    return (os.environ.get("CURSOR_MODEL") or CURSOR_DEFAULT_MODEL) if agent == "cursor" else "haiku"


def agent_requirements(agent: str) -> list[str]:
    """Return actionable local dependency errors, without making paid calls."""
    missing = []
    if agent == "cursor":
        if not os.environ.get("CURSOR_API_KEY"):
            missing.append("CURSOR_API_KEY - run applypilot configure-cursor")
        if importlib.util.find_spec("cursor_sdk") is None:
            missing.append("Cursor SDK - install applypilot[cursor] in your venv")
    elif not shutil.which("claude"):
        missing.append("Claude Code CLI - install from https://claude.ai/code")
    if not shutil.which("npx"):
        missing.append("Node.js / npx - required for Playwright MCP")
    return missing


def redact(text: str) -> str:
    """Remove configured API credentials from external error messages and logs."""
    for name, value in os.environ.items():
        if value and name.endswith(("API_KEY", "API_TOKEN", "PASSWORD")):
            text = text.replace(value, "[redacted]")
    return text
