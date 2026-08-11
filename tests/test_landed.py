"""The landing verb, both faces of it.

`mark_intent_landed` (MCP) and `getflightplan landed` (CLI) both say one thing
to the service: work an intent completed as uncommitted is now in git. These
tests pin what goes on the wire — path, body, bearer — and how a refusal comes
back, because the service treats this as an author-only correction to a
finished record.
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from flightplan import cli, install, landed, mcp_server

INTENT = "3f1a2b3c-0000-4444-8888-aaaabbbbcccc"


# --- the MCP tool -----------------------------------------------------------

def test_the_tool_posts_the_id_and_the_commits(registry, call):
    registry.response = {"intent": {"id": INTENT, "landed_at": "2026-08-11T09:00:00+00:00"}}
    result = call(mcp_server.mark_intent_landed(id=INTENT, commits=["aaa111", "bbb222"]))

    assert registry.paths == [f"/intents/{INTENT}/landed"]
    assert registry.body == {"commits": ["aaa111", "bbb222"]}
    assert registry.auth == ["Bearer test-key"]
    assert result["intent"]["landed_at"]


def test_the_tool_sends_an_empty_body_when_no_commits_are_known(registry, call):
    # "I don't know" is not "there are none": no key, rather than an empty list.
    call(mcp_server.mark_intent_landed(id=INTENT))
    assert registry.body == {}


def test_the_tool_carries_no_paths_and_needs_no_pin(registry, call, tmp_path, monkeypatch):
    # No pin, no git root, no workspace: the call is addressed by id, so none
    # of the routing the other tools do applies to it.
    monkeypatch.chdir(tmp_path)
    call(mcp_server.mark_intent_landed(id=INTENT, commits=["aaa111"]))
    assert registry.paths == [f"/intents/{INTENT}/landed"]
    assert "repo" not in registry.body and "target_id" not in registry.body


@pytest.mark.parametrize("status,detail,expected", [
    (409, "Intent is still active — complete it first; landing is a "
          "post-completion correction.", "complete_intent"),
    (403, "Only the author (sarah) can update this intent.", "author"),
])
def test_a_refusal_comes_back_as_advice_not_an_exception(
    registry, call, status, detail, expected
):
    registry.status = status
    registry.response = {"detail": detail}
    result = call(mcp_server.mark_intent_landed(id=INTENT))

    assert str(status) in result["error"]
    assert detail in result["error"]        # the service's own words survive
    assert expected in result["advice"]


def test_an_unreachable_registry_is_advisory(monkeypatch, call):
    monkeypatch.setenv("FLIGHTPLAN_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FLIGHTPLAN_API_KEY", "test-key")
    result = call(mcp_server.mark_intent_landed(id=INTENT))
    assert "unreachable" in result["error"]
    assert "advisory" in result["advice"]


def test_the_tool_is_registered_with_the_others(call):
    names = sorted(t.name for t in call(mcp_server.mcp.list_tools()))
    assert names == [
        "complete_intent", "list_intents", "mark_intent_landed",
        "post_intent", "update_intent",
    ]


# --- the body rule, shared -------------------------------------------------

BODIES = [
    (None, {}),                                   # never reported
    ([], {"commits": []}),                        # reported: none
    (["aaa111"], {"commits": ["aaa111"]}),
    ([" aaa111 ", ""], {"commits": ["aaa111"]}),  # stripped, blanks dropped
    (["  "], {"commits": []}),                    # reported, all blank
]


@pytest.mark.parametrize("commits,expected", BODIES)
def test_the_body_rule_is_one_rule(commits, expected):
    assert landed.body_for(commits) == expected


# Every row above except a literal empty list, which argparse cannot produce:
# `--commit ""` is how the command says "reported: none", and the blank row
# below covers it.
CLI_BODIES = [row for row in BODIES if row[0] != []]


@pytest.mark.parametrize("commits,expected", CLI_BODIES)
def test_both_clients_send_the_same_body(signed_in, registry, call, commits, expected):
    """The drift guard: the tool and the command must make the same statement
    for the same input, or one of them reports commits the caller never gave."""
    call(mcp_server.mark_intent_landed(id=INTENT, commits=commits))
    assert registry.body == expected

    server, url = stub_service({
        f"/intents/{INTENT}/landed": lambda body: (200, {"intent": {"landed_at": "now"}}),
    })
    argv = [INTENT, "--url", url]
    for sha in commits or []:
        argv += ["--commit", sha]
    try:
        assert landed.main(argv) == 0
    finally:
        server.shutdown()
    assert server.calls[0][1] == expected


# --- the CLI ----------------------------------------------------------------

def stub_service(routes: dict):
    """The same shape tests/test_register.py uses: a path maps to a function of
    the request body returning (status, reply). Every request is recorded as
    (path, body, auth)."""
    calls: list[tuple[str, dict, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            calls.append((self.path, body, self.headers.get("Authorization") or ""))
            status, reply = routes[self.path](body)
            payload = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # keep test output quiet
            pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), Handler)
    server.calls = calls
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    """A private HOME holding a stored credential, and a cwd with no pin."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLIGHTPLAN_API_KEY", raising=False)
    monkeypatch.delenv("FLIGHTPLAN_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    install._write_key_file("flpd_secret")
    return home


def test_the_cli_sends_the_explicit_shas(signed_in, capsys):
    server, url = stub_service({
        f"/intents/{INTENT}/landed": lambda body: (
            200, {"intent": {"id": INTENT, "landed_at": "2026-08-11T09:00:00+00:00"}}
        ),
    })
    try:
        rc = landed.main([INTENT, "--commit", "aaa111", "--commit", "bbb222",
                          "--url", url])
    finally:
        server.shutdown()

    assert rc == 0
    path, body, auth = server.calls[0]
    assert path == f"/intents/{INTENT}/landed"
    assert body == {"commits": ["aaa111", "bbb222"]}
    assert auth == "Bearer flpd_secret"
    assert "2026-08-11T09:00:00+00:00" in capsys.readouterr().out


def test_the_cli_lands_without_shas(signed_in, capsys):
    # The timestamp is the correction; the client never guesses the commits.
    # No flag means no report — the same empty body the MCP tool sends.
    server, url = stub_service({
        f"/intents/{INTENT}/landed": lambda body: (200, {"intent": {"landed_at": "now"}}),
    })
    try:
        rc = landed.main([INTENT, "--url", url])
    finally:
        server.shutdown()

    assert rc == 0
    assert server.calls[0][1] == {}
    assert "Landed" in capsys.readouterr().out


def test_the_cli_prints_the_service_message_and_exits_nonzero(signed_in, capsys):
    server, url = stub_service({
        f"/intents/{INTENT}/landed": lambda body: (
            409, {"detail": "Intent is still active — complete it first."}
        ),
    })
    try:
        rc = landed.main([INTENT, "--url", url])
    finally:
        server.shutdown()

    assert rc == 1
    assert "still active" in capsys.readouterr().out


def test_service_text_cannot_paint_the_terminal(signed_in, capsys):
    """A detail can quote text people chose (an author handle, a repo name),
    and a custom service can send anything at all. Escapes go before printing."""
    nasty = "Only the author (\x1b[2K\rsudo rm -rf /\u202e) can update this."
    server, url = stub_service({
        f"/intents/{INTENT}/landed": lambda body: (403, {"detail": nasty}),
    })
    try:
        assert landed.main([INTENT, "--url", url]) == 1
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out and "\u202e" not in out
    assert "Only the author" in out and "sudo rm -rf /" in out   # readable, inert


def test_a_hostile_timestamp_is_scrubbed_too(signed_in, capsys):
    server, url = stub_service({
        f"/intents/{INTENT}/landed": lambda body: (
            200, {"intent": {"landed_at": "2026-08-11\x1b[31mFAKE\x07"}}
        ),
    })
    try:
        assert landed.main([INTENT, "--url", url]) == 0
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert "2026-08-11" in out


def test_the_cli_needs_a_credential(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLIGHTPLAN_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    assert landed.main([INTENT]) == 1
    assert "getflightplan login" in capsys.readouterr().out


def test_a_dead_service_exits_nonzero(signed_in, capsys):
    assert landed.main([INTENT, "--url", "http://127.0.0.1:1"]) == 1
    assert "did not answer" in capsys.readouterr().out


def test_the_front_door_dispatches_landed(signed_in, capsys):
    server, url = stub_service({
        f"/intents/{INTENT}/landed": lambda body: (200, {"intent": {"landed_at": "now"}}),
    })
    try:
        rc = cli.main(["landed", INTENT, "--commit", "aaa111", "--url", url])
    finally:
        server.shutdown()

    assert rc == 0
    assert server.calls[0][1] == {"commits": ["aaa111"]}
    capsys.readouterr()
