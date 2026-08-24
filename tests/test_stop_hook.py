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

PROJECT_PIN = (
    'target = "project"\ntarget_id = "proj_5b71ee"\n'
    'name = "flightplan"\nurl = "http://unused"\n'
)


def run_hook(stdin: dict, url: str, cwd: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, SCRIPT],
        input=json.dumps(stdin),
        capture_output=True, text=True, timeout=15, cwd=cwd,
        env={
            "PATH": "/usr/bin:/bin", "FLIGHTPLAN_URL": url, "FLIGHTPLAN_API_KEY": "k",
            # Keep the block-memory state file inside the test dir, not ~/.cache.
            "XDG_CACHE_HOME": cwd,
        },
    )
    return proc.returncode, proc.stdout


def stub_registry(intents: list[dict] | None, status: int = 200, only_if=None):
    """A one-endpoint HTTP stub standing in for GET /intents. Records every
    request path on `server.requests` so tests can assert what was queried.

    `only_if` is a predicate over the request path: when given, the intents come
    back only for a matching query, which is how a test stands in for a filter
    the real registry applies.
    """
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            found = intents if (only_if is None or only_if(self.path)) else []
            body = json.dumps({"intents": found or []}).encode()
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


def test_never_loops_without_the_flag(tmp_path):
    # Cursor runs these hooks but never sends stop_hook_active, so the flag
    # alone let the block repeat forever. The on-disk guard catches the
    # repeat: same session, same intent set — block once, then allow.
    server, url = stub_registry([{"id": "y" * 36, "author": "brad", "summary": "s"}])
    try:
        payload = {"session_id": "sess-1"}
        code, out = run_hook(payload, url, str(tmp_path))
        assert code == 0 and json.loads(out)["decision"] == "block"
        for _ in range(2):  # every later stop passes, not just the next one
            code, out = run_hook(payload, url, str(tmp_path))
            assert code == 0 and out.strip() == ""
    finally:
        server.shutdown()


def test_block_memory_is_per_session_and_per_intent_set(tmp_path):
    server, url = stub_registry([{"id": "y" * 36, "author": "brad", "summary": "s"}])
    try:
        code, out = run_hook({"session_id": "sess-1"}, url, str(tmp_path))
        assert json.loads(out)["decision"] == "block"
        # A different session still gets its own one nag.
        code, out = run_hook({"session_id": "sess-2"}, url, str(tmp_path))
        assert json.loads(out)["decision"] == "block"
    finally:
        server.shutdown()
    # A changed intent set is fresh news: sess-1 gets nagged again.
    server, url = stub_registry([{"id": "z" * 36, "author": "brad", "summary": "s"}])
    try:
        code, out = run_hook({"session_id": "sess-1"}, url, str(tmp_path))
        assert json.loads(out)["decision"] == "block"
    finally:
        server.shutdown()


def test_never_loops_with_an_empty_payload(tmp_path):
    # A harness that identifies the session in no way we know: the guard
    # degrades to cwd-keyed memory and still breaks the loop.
    server, url = stub_registry([{"id": "y" * 36, "author": "brad", "summary": "s"}])
    try:
        code, out = run_hook({}, url, str(tmp_path))
        assert code == 0 and json.loads(out)["decision"] == "block"
        code, out = run_hook({}, url, str(tmp_path))
        assert code == 0 and out.strip() == ""
    finally:
        server.shutdown()


def test_cursor_payload_keys_per_conversation(tmp_path):
    # The payload Cursor actually sends: conversation_id, no session_id, no
    # stop_hook_active. One nag per chat, then quiet; another chat in the
    # same repo still gets its own nag.
    server, url = stub_registry([{"id": "y" * 36, "author": "brad", "summary": "s"}])
    try:
        code, out = run_hook(
            {"conversation_id": "conv-1", "loop_count": 1}, url, str(tmp_path)
        )
        assert code == 0 and json.loads(out)["decision"] == "block"
        code, out = run_hook(
            {"conversation_id": "conv-1", "loop_count": 2}, url, str(tmp_path)
        )
        assert code == 0 and out.strip() == ""
        code, out = run_hook({"conversation_id": "conv-2"}, url, str(tmp_path))
        assert code == 0 and json.loads(out)["decision"] == "block"
    finally:
        server.shutdown()


def test_claude_code_later_stop_still_nags(tmp_path):
    # Claude Code sends the stop_hook_active KEY on every stop — false on
    # each fresh attempt. There the flag is the only guard: a later stop in
    # the same conversation with the same open intents must block again
    # (ROADMAP 9 — the end-of-session nag is the point), never be silenced
    # by the disk memory.
    server, url = stub_registry([{"id": "y" * 36, "author": "brad", "summary": "s"}])
    try:
        for _ in range(2):
            code, out = run_hook(
                {"stop_hook_active": False, "session_id": "sess-1"}, url, str(tmp_path)
            )
            assert code == 0 and json.loads(out)["decision"] == "block"
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


def _asks_under(tmp_path, pin_text: str) -> list[str]:
    """Run the hook against a stub with the given pin file in place; return the
    request paths so a test can see which name it asked about."""
    (tmp_path / ".flightplan.toml").write_text(pin_text)
    server, url = stub_registry([{"id": "d" * 36, "author": "brad", "summary": "s"}])
    try:
        code, _out = run_hook({"stop_hook_active": False}, url, str(tmp_path))
        assert code == 0
        return list(server.requests)
    finally:
        server.shutdown()


def test_pinned_repo_name_from_toml(tmp_path):
    # A .flightplan.toml pin (cwd or any parent) is what the hook asks
    # under — the whole point of the install kit is that every agent agrees.
    # Nothing to add on the older shape: name only.
    requests = _asks_under(
        tmp_path,
        '# pinned by the installer\nrepo = "pinned-name"\nurl = "http://unused"\n',
    )
    assert any("repo=pinned-name" in path for path in requests)
    assert not any("target_id" in path for path in requests)


def test_pinned_id_is_sent_with_the_name(tmp_path):
    # The newer shape carries the id next to the readable name. The hook sends
    # both: the id so a drifted name can't point the check at the wrong repo,
    # the name so a registry that predates the id still filters.
    requests = _asks_under(
        tmp_path,
        'target = "repository"\ntarget_id = "repo_9f3c2a"\n'
        'name = "pinned-name"\nurl = "http://unused"\n',
    )
    assert any("repo=pinned-name" in path for path in requests)
    assert any("target_id=repo_9f3c2a" in path for path in requests)
    # A repository pin asks exactly what it always asked: no subtree to widen to.
    assert not any("subtree" in path for path in requests)


def test_a_project_pin_asks_for_the_whole_subtree(tmp_path):
    # A project id matches only intents posted at the project, and an exact
    # filter would miss work demoted into a child repo. `subtree=1` widens it.
    requests = _asks_under(tmp_path, PROJECT_PIN)
    assert any("repo=flightplan" in path for path in requests)
    assert any("target_id=proj_5b71ee" in path for path in requests)
    assert any("subtree=1" in path for path in requests)


def test_an_intent_demoted_into_a_child_repo_still_blocks(tmp_path):
    # The case the subtree query exists for: this session's only open intent
    # lives on a CHILD target, so the stub answers only the widened query. A
    # hook that asked the narrow one would see nothing and lose the outcome.
    (tmp_path / ".flightplan.toml").write_text(PROJECT_PIN)
    server, url = stub_registry(
        [{"id": "e" * 36, "author": "brad", "summary": "Work that landed in one child repo."}],
        only_if=lambda path: "subtree=1" in path,
    )
    try:
        code, out = run_hook({"stop_hook_active": False}, url, str(tmp_path))
        assert code == 0
        assert json.loads(out)["decision"] == "block"
    finally:
        server.shutdown()


def test_hook_pin_parser_matches_the_package(tmp_path):
    # The hook ships standalone (system python3, no imports from the package),
    # so it carries its own copy of the pin logic. The two must not drift.
    import importlib.util

    from flightplan import config

    spec = importlib.util.spec_from_file_location("_hook", SCRIPT)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    samples = [
        'repo = "legacy"\nurl = "http://u"\n',
        'target = "repository"\ntarget_id = "repo_1"\nname = "newer"\nurl = "http://u"\n',
        'target = "project"\ntarget_id = "proj_1"\nname = "a project"\n',
        'repo = "old"\nname = "new"\n',
    ]
    for text in samples:
        pin_file = tmp_path / ".flightplan.toml"
        pin_file.write_text(text)
        expected = config.read_pin(text)
        from_hook = hook._toml_value(pin_file, "name") or hook._toml_value(pin_file, "repo")
        assert from_hook == expected.name
        assert hook._toml_value(pin_file, "target_id") == expected.target_id
        # `target` too, now that it decides whether the check spans a subtree.
        assert hook._toml_value(pin_file, "target") == expected.target
