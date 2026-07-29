"""Stop-hook (ROADMAP item 9): blocks a session stop once when the repo has
active intents; allows it on any failure, on the retry, and when the feed is
quiet. The script is exercised as a subprocess against a stub registry — the
same way Claude Code runs it."""

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = str(
    Path(__file__).resolve().parents[1]
    / "src" / "flightplan" / "install_assets" / "stop_hook.py"
)


def run_hook(stdin: dict, url: str, cwd: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, SCRIPT],
        input=json.dumps(stdin),
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "FLIGHTPLAN_URL": url, "FLIGHTPLAN_API_KEY": "k"},
    )
    return proc.returncode, proc.stdout


def stub_registry(intents: list[dict] | None, status: int = 200):
    """A one-endpoint HTTP stub standing in for GET /intents. Records every
    request path on `server.requests` so tests can assert what was queried."""
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = json.dumps({"intents": intents or []}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep test output quiet
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), Handler)
    server.requests = requests
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def test_blocks_once_when_repo_has_active_intents(tmp_path):
    server, url = stub_registry([{
        "id": "abcd1234-0000-0000-0000-000000000000",
        "author": "brad",
        "summary": "Long-running refactor of the ingest pipeline.",
    }])
    try:
        code, out = run_hook({"stop_hook_active": False}, url, str(tmp_path))
        assert code == 0
        decision = json.loads(out)
        assert decision["decision"] == "block"
        assert "abcd1234" in decision["reason"]
        assert 'session="current"' in decision["reason"]
    finally:
        server.shutdown()


def test_allows_stop_when_quiet(tmp_path):
    server, url = stub_registry([])
    try:
        code, out = run_hook({"stop_hook_active": False}, url, str(tmp_path))
        assert code == 0 and out.strip() == ""
    finally:
        server.shutdown()


def test_never_loops(tmp_path):
    # Second stop after a block: stop_hook_active is set — always allow.
    server, url = stub_registry([{"id": "x" * 36, "author": "brad", "summary": "s"}])
    try:
        code, out = run_hook({"stop_hook_active": True}, url, str(tmp_path))
        assert code == 0 and out.strip() == ""
    finally:
        server.shutdown()


def test_registry_failure_allows_stop(tmp_path):
    # Advisory rule: an erroring registry must never trap a session.
    server, url = stub_registry(None, status=500)
    try:
        code, out = run_hook({"stop_hook_active": False}, url, str(tmp_path))
        assert code == 0 and out.strip() == ""
    finally:
        server.shutdown()
    # Unreachable registry (nothing listening) — same posture.
    code, out = run_hook({"stop_hook_active": False}, "http://127.0.0.1:1", str(tmp_path))
    assert code == 0 and out.strip() == ""


def test_missing_config_allows_stop(tmp_path):
    proc = subprocess.run(
        [sys.executable, SCRIPT], input="{}", capture_output=True, text=True,
        timeout=15, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_pinned_repo_name_from_toml(tmp_path):
    # A .flightplan.toml pin (cwd or any parent) is what the hook asks
    # under — the whole point of the install kit is that every agent agrees.
    (tmp_path / ".flightplan.toml").write_text(
        '# pinned by the installer\nrepo = "pinned-name"\nurl = "http://unused"\n'
    )
    server, url = stub_registry([{"id": "d" * 36, "author": "brad", "summary": "s"}])
    try:
        code, out = run_hook({"stop_hook_active": False}, url, str(tmp_path))
        assert code == 0
        assert any("repo=pinned-name" in path for path in server.requests)
    finally:
        server.shutdown()
