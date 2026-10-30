"""Streaming deadlines, process cleanup, and cancellation."""

import os
import subprocess
import sys
import threading
import time

import pytest

from applypilot.apply.agents import cancel_all, stream_agent


@pytest.mark.parametrize(
    "script",
    [
        "import time; time.sleep(60)",
        "import time; exec('while True:\\n print(\"{}\", flush=True)')",
        "import sys,time; sys.stdin.read(); time.sleep(60)",
    ],
)
def test_deadline_applies_to_silent_and_busy_processes(tmp_path, script):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        list(stream_agent([sys.executable, "-u", "-c", script], "input", tmp_path, 0, 0.4))
    assert time.monotonic() - started < 5


def test_skip_cancels_process_without_waiting_for_deadline(tmp_path):
    script = 'import json,os,time; print(json.dumps({"type":"pid", "pid":os.getpid()}),flush=True); time.sleep(60)'
    events = []

    def consume():
        events.extend(stream_agent([sys.executable, "-u", "-c", script], "", tmp_path, 0, 60))

    worker = threading.Thread(target=consume)
    worker.start()
    deadline = time.monotonic() + 5
    while not events and time.monotonic() < deadline:
        time.sleep(0.02)
    # list.extend is incremental, so the first PID event proves registration.
    assert events
    cancel_all()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert events[-1]["type"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(events[0]["pid"], 0)


def test_real_sdk_bridge_health_without_credentials(tmp_path):
    from cursor_sdk import CursorClient

    with CursorClient.launch_bridge(workspace=tmp_path) as client:
        assert client.ping()
        assert client.get_version()["bridgeVersion"]
