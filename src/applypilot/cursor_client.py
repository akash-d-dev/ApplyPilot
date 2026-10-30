"""Cursor's official Python SDK behind ApplyPilot's text client interface."""

import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from applypilot.providers import CURSOR_DEFAULT_MODEL, redact

log = logging.getLogger(__name__)


def cursor_model_selection(model: str):
    """Pin Composer's standard mode instead of inheriting the account's Fast default."""
    if model != 'composer-2.5':
        return model
    from cursor_sdk import ModelParameterValue, ModelSelection
    return ModelSelection(id=model, params=[ModelParameterValue(id='fast', value='false')])


@contextmanager
def cursor_session(workspace: Path):
    """Own the bridge explicitly so threads and jobs never share SDK state."""
    try:
        from cursor_sdk import CursorClient
        from cursor_sdk.errors import CursorAgentError
    except ImportError as exc:
        raise RuntimeError("Install Cursor support: python -m pip install 'applypilot[cursor]'") from exc
    try:
        with CursorClient.launch_bridge(workspace=workspace, max_retries=0) as client:
            yield client
    except (CursorAgentError, OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"Cursor request failed: {redact(str(exc))}") from None


def validate_model(client, model: str) -> None:
    """Fail before processing jobs when the key cannot access the chosen model."""
    models = client.models.list(api_key=os.environ.get("CURSOR_API_KEY", ""))
    if model not in {entry.id for entry in models}:
        raise RuntimeError(
            f"Cursor model '{model}' is unavailable for this API key. Set CURSOR_MODEL to an available ID."
        )


class CursorLLMClient:
    """Use a fresh, tool-free Cursor agent for each text request.

    temperature and max_tokens retain call compatibility but are not SDK
    sampling controls. Existing stage validators still validate responses.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("LLM_MODEL") or os.environ.get("CURSOR_MODEL") or CURSOR_DEFAULT_MODEL
        self._validated = False
        self._lock = threading.Lock()

    def validate(self) -> None:
        """Check authentication and model access without an inference request."""
        with self._lock:
            if not self._validated:
                with (
                    tempfile.TemporaryDirectory(prefix="applypilot-cursor-") as directory,
                    cursor_session(Path(directory)) as client,
                ):
                    validate_model(client, self.model)
                self._validated = True
                if self.model == 'composer-2.5':
                    log.info('Cursor model: composer-2.5 (fast=false)')

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4096) -> str:
        """Return only the terminal assistant response, with no workspace tools."""
        self.validate()
        from cursor_sdk import AgentOptions, LocalAgentOptions

        instruction = (
            "Respond to the conversation below. Follow its system instructions. "
            "Return only the requested answer, without agent progress commentary.\n"
            + json.dumps(messages, ensure_ascii=False)
        )
        with (
            tempfile.TemporaryDirectory(prefix="applypilot-cursor-") as directory,
            cursor_session(Path(directory)) as client,
            client.agents.create(
                AgentOptions(
                    model=cursor_model_selection(self.model),
                    api_key=os.environ.get("CURSOR_API_KEY", ""),
                    tools=[],
                    local=LocalAgentOptions(cwd=directory, setting_sources=[]),
                )
            ) as agent,
        ):
            run = agent.send(instruction)
            deadline = threading.Timer(120, run.cancel)
            deadline.daemon = True
            deadline.start()
            try:
                result = run.wait()
            finally:
                deadline.cancel()
            if result.status != "finished" or not result.result.strip():
                raise RuntimeError(f"Cursor text run {result.status}: {result.error_message or 'empty response'}")
            return result.result

    def ask(self, prompt: str, **kwargs) -> str:
        """Send a single user prompt."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        """All bridges and agents close at the end of each request."""
