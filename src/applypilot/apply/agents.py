"""Browser agent commands, streaming deadlines, and process lifecycle."""

import json
import os
import platform
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from applypilot.apply.chrome import _kill_process_tree
from applypilot.providers import redact

_processes: dict[int, subprocess.Popen] = {}
_lock = threading.RLock()
_cancelled: set[int] = set()


def build_command(agent: str, model: str, mcp_path: Path, dry_run: bool) -> list[str]:
    """Build either command without placing prompts or credentials in argv."""
    if agent == "cursor":
        command = [
            sys.executable,
            "-m",
            "applypilot.apply.cursor_runner",
            "--model",
            model,
            "--mcp-config",
            str(mcp_path),
        ]
        return command + (["--dry-run"] if dry_run else [])
    denied = [
        "draft_email",
        "modify_email",
        "delete_email",
        "download_attachment",
        "batch_modify_emails",
        "batch_delete_emails",
        "create_label",
        "update_label",
        "delete_label",
        "get_or_create_label",
        "list_email_labels",
        "create_filter",
        "list_filters",
        "get_filter",
        "delete_filter",
    ]
    if dry_run:
        denied.append("send_email")
    return [
        "claude",
        "--model",
        model,
        "-p",
        "--mcp-config",
        str(mcp_path),
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools",
        ",".join(f"mcp__gmail__{tool}" for tool in denied),
        "--output-format",
        "stream-json",
        "--verbose",
        "-",
    ]


def cancel_all() -> None:
    """Signal active agents; stream owners perform bounded cleanup."""
    with _lock:
        _cancelled.update(_processes)
        processes = list(_processes.values())
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    # Descendants can outlive their root; the process owns a separate group.
    if platform.system() != "Windows":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        _kill_process_tree(process.pid)
    process.wait(timeout=5)


def stream_agent(command: list[str], prompt: str, workspace: Path, worker_id: int, timeout: float) -> Iterator[dict]:
    """Read events without letting a silent or busy stdout bypass the deadline."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    events: queue.Queue = queue.Queue(maxsize=256)
    finished = threading.Event()
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=workspace,
        env=env,
        start_new_session=platform.system() != "Windows",
    )
    with _lock:
        _processes[worker_id] = process

    def enqueue(item) -> None:
        while not finished.is_set():
            try:
                events.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def read() -> None:
        try:
            for line in process.stdout:
                enqueue(line)
        finally:
            enqueue(None)

    def write() -> None:
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    reader = threading.Thread(target=read, daemon=True)
    writer = threading.Thread(target=write, daemon=True)
    reader.start()
    writer.start()
    try:
        while True:
            with _lock:
                cancelled = worker_id in _cancelled
            if cancelled:
                yield {"type": "cancelled"}
                return
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                line = events.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event
        returncode = process.wait(timeout=max(0.01, timeout - (time.monotonic() - started)))
        if returncode < 0:
            yield {"type": "cancelled"}
        elif returncode:
            raise RuntimeError(f"Agent process exited with code {returncode}.")
    finally:
        finished.set()
        _stop(process)
        reader.join(timeout=2)
        writer.join(timeout=2)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()
        with _lock:
            _processes.pop(worker_id, None)
            _cancelled.discard(worker_id)


def result_status(output: str, dry_run: bool) -> str:
    """Accept one terminal result marker, never a quoted intermediate marker."""
    import re

    lines = [line.strip().strip("*`").strip() for line in output.splitlines()]
    markers = []
    for line in lines:
        match = re.fullmatch(r"RESULT:(APPLIED|PREVIEWED|EXPIRED|CAPTCHA|LOGIN_ISSUE|FAILED(?::.+)?)", line)
        if match:
            markers.append(match[1])
    if len(markers) != 1:
        return "failed:invalid_result_line"
    marker = markers[0].strip()
    if dry_run and marker == "APPLIED":
        return "failed:unexpected_submission_in_preview"
    if not dry_run and marker == "PREVIEWED":
        return "failed:submission_not_confirmed"
    return redact(marker.lower())
