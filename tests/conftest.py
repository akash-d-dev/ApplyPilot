"""Hermetic fixtures: synthetic applicant data and temporary application storage."""

import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from applypilot import config, database


@pytest.fixture(autouse=True)
def isolated_setup(monkeypatch, tmp_path):
    for name in (
        "CURSOR_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "LLM_URL",
        "LLM_API_KEY",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "CURSOR_MODEL",
        "APPLY_AGENT",
        "JOB_PORTAL_SIGNUP",
        "JOB_PORTAL_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    for name, relative in {
        "DB_PATH": "jobs.db",
        "ENV_PATH": ".env",
        "PROFILE_PATH": "profile.json",
        "RESUME_PATH": "resume.txt",
        "RESUME_PDF_PATH": "resume.pdf",
        "SEARCH_CONFIG_PATH": "searches.yaml",
        "TAILORED_DIR": "tailored",
        "COVER_LETTER_DIR": "covers",
        "LOG_DIR": "logs",
        "CHROME_WORKER_DIR": "chrome-workers",
        "APPLY_WORKER_DIR": "apply-workers",
    }.items():
        monkeypatch.setattr(config, name, tmp_path / relative)
    monkeypatch.setattr(database, "DB_PATH", config.DB_PATH)
    from applypilot import llm
    from applypilot.apply import launcher

    launcher._skipped_urls.clear()
    launcher._stop_event.clear()

    monkeypatch.setattr(llm, "_instance", None)
    config.ensure_dirs()
    yield
    database.close_connection()


@pytest.fixture
def applicant():
    source = Path(__file__).parents[1] / "profile.example.json"
    profile = json.loads(source.read_text())
    profile["personal"].update(
        full_name="Synthetic Test Applicant",
        preferred_name="Synthetic",
        email="applicant@example.test",
        password="",
        city="Test City",
    )
    config.PROFILE_PATH.write_text(json.dumps(profile))
    return profile


@pytest.fixture
def jobs(applicant, tmp_path):
    resume = tmp_path / "synthetic.txt"
    resume.write_text(
        "Synthetic Test Applicant\nTest Engineer\napplicant@example.test\nSUMMARY\nSynthetic fixture only."
    )
    from applypilot.scoring.pdf import convert_to_pdf

    pdf = convert_to_pdf(resume)
    conn = database.init_db()

    def add(url="http://application.test/", status=None):
        conn.execute(
            """INSERT INTO jobs
            (url, application_url, title, site, fit_score, tailored_resume_path, full_description, apply_status)
            VALUES (?, ?, 'Test Engineer', 'Fixture', 9, ?, 'Remote test role', ?)""",
            (url, url, str(pdf), status),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone())

    return add


@pytest.fixture
def application_site():
    html = (Path(__file__).parent / "fixtures" / "application.html").read_bytes()
    submissions = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            submissions.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Application received")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/", submissions
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def browser_worker(monkeypatch, tmp_path):
    """Launch a fresh test browser on an unused port, without cloning user data."""
    from applypilot.apply import launcher

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(launcher, "BASE_CDP_PORT", port)

    def launch(worker_id, port, headless):
        process = subprocess.Popen(
            [
                config.get_chrome_path(),
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={tmp_path / 'browser'}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/json/version").is_success:
                    return process
            except httpx.HTTPError:
                time.sleep(0.1)
        from applypilot.apply.chrome import cleanup_worker

        cleanup_worker(worker_id, process)
        raise RuntimeError("Fixture browser did not start")

    monkeypatch.setattr(launcher, "launch_chrome", launch)
    return port
