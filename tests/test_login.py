"""`getflightplan login` (decisions 61822208 and b18cf641).

The service is a stub HTTP server, the same idiom the stop-hook tests use. The
browser is a stub too: it does what a real browser does with the authorize
address, which is to call the loopback listener back with a code.

The rule these tests protect: the credential goes to the env file with mode
600, and it never reaches stdout.
"""

import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from flightplan import cli, config, install, login

# A stand-in token. Declared once, so no test line looks like a real secret.
_FAKE_TOKEN = "fp-fake-credential-0000"


# --------------------------------------------------------------------------- #
# Stub service
# --------------------------------------------------------------------------- #

def stub_service(routes: dict):
    """An HTTP stub for the auth endpoints.

    `routes` maps a path to a function of the request body. The function
    returns (status, response body). Every request is recorded on
    `server.calls` as (path, body), which is how a test asserts what went out.
    """
    calls: list[tuple[str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            calls.append((self.path, body))
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


def _home(tmp_path, monkeypatch):
    """A private HOME, so the env file of the developer is never touched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLIGHTPLAN_API_KEY", raising=False)
    return home / ".config" / "flightplan" / "env"


def _sequence(*answers):
    """A route that gives each answer in turn, then repeats the last one."""
    remaining = list(answers)

    def route(_body):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return route


# --------------------------------------------------------------------------- #
# PKCE
# --------------------------------------------------------------------------- #

def test_pkce_challenge_is_unpadded_base64url_sha256():
    import base64
    import hashlib

    verifier, challenge = login.pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    assert challenge == expected
    assert "=" not in challenge


def test_pkce_verifier_length_and_alphabet():
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    seen = set()
    for _ in range(5):
        verifier, _challenge = login.pkce_pair()
        assert 43 <= len(verifier) <= 128
        assert set(verifier) <= allowed
        seen.add(verifier)
    assert len(seen) == 5  # a fresh verifier every time


# --------------------------------------------------------------------------- #
# The loopback listener
# --------------------------------------------------------------------------- #

def test_listener_binds_loopback_on_an_ephemeral_port():
    server = login.make_listener()
    try:
        host, port = server.server_address
        assert host == "127.0.0.1"   # never localhost, never 0.0.0.0
        assert port > 0
        assert login.redirect_uri(server) == f"http://127.0.0.1:{port}/callback"
    finally:
        server.server_close()


def test_listener_closes_after_the_callback():
    server = login.make_listener()
    port = server.server_address[1]
    threading.Thread(
        target=lambda: urllib.request.urlopen(
            f"http://127.0.0.1:{port}/callback?code=one-time", timeout=5
        ).read(),
        daemon=True,
    ).start()

    assert login.wait_for_code(server, timeout=10) == "one-time"

    # The port is closed: nothing answers a second request.
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback", timeout=2)


def test_callback_timeout_advises_the_headless_flow():
    server = login.make_listener()
    ticks = iter([0.0, 0.0, 999.0, 999.0])
    with pytest.raises(login.LoginError) as err:
        login.wait_for_code(server, timeout=5, clock=lambda: next(ticks))
    assert "--headless" in str(err.value)


# --------------------------------------------------------------------------- #
# Rotation: a login replaces the credential the machine already holds
# --------------------------------------------------------------------------- #

def test_a_stored_device_token_yields_its_credential_id(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    install._write_key_file("flpd_abc123_secretpart")
    assert login.stored_credential_id() == "cred_abc123"


def test_a_legacy_key_yields_no_credential_id(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    install._write_key_file("legacy-env-key")
    assert login.stored_credential_id() is None


def test_no_stored_key_yields_no_credential_id(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert login.stored_credential_id() is None


def test_exchange_carries_replaces_only_when_it_has_one():
    server, url = stub_service({
        "/auth/device": lambda body: (200, {"token": _FAKE_TOKEN}),
    })
    try:
        login.exchange(url, "code-1", "v" * 43, replaces="cred_abc123")
        login.exchange(url, "code-2", "v" * 43)
    finally:
        server.shutdown()
    first, second = server.calls[0][1], server.calls[1][1]
    assert first["replaces"] == "cred_abc123"
    assert "replaces" not in second


def test_browser_flow_replaces_the_stored_credential(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    install._write_key_file("flpd_oldhex_oldsecret")
    seen: dict = {}
    server, url = stub_service({
        "/auth/device": lambda body: (200, {"token": _FAKE_TOKEN}),
    })
    try:
        monkeypatch.setenv("FLIGHTPLAN_URL", url)
        monkeypatch.setattr(login.webbrowser, "open", _browser_that_approves(seen))
        assert login.main([]) == 0
    finally:
        server.shutdown()
    (_path, body), = server.calls
    assert body["replaces"] == "cred_oldhex"


# --------------------------------------------------------------------------- #
# Browser flow, end to end
# --------------------------------------------------------------------------- #

def _browser_that_approves(seen: dict, code: str = "one-time-code"):
    """Stands in for the browser and for the redirect of the service."""
    def opener(target: str) -> bool:
        seen["target"] = target
        query = parse_qs(urlsplit(target).query)
        seen["query"] = query
        back = f"{query['redirect_uri'][0]}?code={code}"
        threading.Thread(
            target=lambda: urllib.request.urlopen(back, timeout=5).read(),
            daemon=True,
        ).start()
        return True

    return opener


def test_browser_flow_stores_the_credential(tmp_path, monkeypatch, capsys):
    env_file = _home(tmp_path, monkeypatch)
    seen: dict = {}

    def device(body):
        seen["exchange"] = body
        return 200, {
            "token": _FAKE_TOKEN,
            "credential_id": "cred_7ab3",
            "label": "laptop",
        }

    server, url = stub_service({"/auth/device": device})
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    monkeypatch.setattr(login.webbrowser, "open", _browser_that_approves(seen))
    try:
        assert login.main([]) == 0
    finally:
        server.shutdown()

    # The authorize address carries the challenge for our verifier, a
    # 127.0.0.1 callback, and a label.
    query = seen["query"]
    assert seen["target"].startswith(f"{url}/cli/authorize?")
    assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert query["redirect_uri"][0].endswith("/callback")
    assert query["label"][0]

    # The exchange carries the code from the redirect and the matching verifier.
    exchanged = seen["exchange"]
    assert exchanged["code"] == "one-time-code"
    _v, challenge = exchanged["verifier"], query["challenge"][0]
    import base64
    import hashlib
    assert challenge == base64.urlsafe_b64encode(
        hashlib.sha256(_v.encode()).digest()
    ).decode().rstrip("=")

    # The credential lands in the env file, with mode 600.
    assert env_file.read_text() == f"FLIGHTPLAN_API_KEY={_FAKE_TOKEN}\n"
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert (env_file.parent.stat().st_mode & 0o777) == 0o700

    # It is reported, but never printed.
    out = capsys.readouterr().out
    assert _FAKE_TOKEN not in out
    assert str(env_file) in out
    assert "cred_7ab3" in out and "laptop" in out


def test_browser_flow_writes_nothing_into_the_repository(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".flightplan.toml").write_text('repo = "demo"\nurl = "http://unused"\n')
    before = (repo / ".flightplan.toml").read_text()
    monkeypatch.chdir(repo)

    server, url = stub_service({
        "/auth/device": lambda _b: (200, {"token": _FAKE_TOKEN, "credential_id": "c1"}),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    monkeypatch.setattr(login.webbrowser, "open", _browser_that_approves({}))
    try:
        assert login.main([]) == 0
    finally:
        server.shutdown()

    assert (repo / ".flightplan.toml").read_text() == before
    assert sorted(p.name for p in repo.iterdir()) == [".flightplan.toml"]


# --------------------------------------------------------------------------- #
# Headless flow
# --------------------------------------------------------------------------- #

_STARTED = {
    "device_code": "dev-1234",
    "user_code": "WXYZ-99",
    "verification_uri": "https://example.test/cli",
    "interval": 7,
    "expires_in": 600,
}


def test_headless_flow_polls_then_stores(tmp_path, monkeypatch, capsys):
    env_file = _home(tmp_path, monkeypatch)
    polls: list[dict] = []
    slept: list[float] = []

    def device(body):
        polls.append(body)
        if len(polls) < 3:
            return 400, {"error": "authorization_pending"}
        return 200, {"token": _FAKE_TOKEN, "credential_id": "cred_9", "label": "box"}

    server, url = stub_service({
        "/auth/device/start": lambda body: (200, _STARTED),
        "/auth/device": device,
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    monkeypatch.setattr(login.webbrowser, "open", lambda _t: pytest.fail("no browser"))
    try:
        assert login.main(["--headless"], sleep=slept.append) == 0
    finally:
        server.shutdown()

    # The start call carries the challenge and the label.
    start_body = server.calls[0][1]
    assert set(start_body) == {"challenge", "label"}

    # Every poll carries the device code and the verifier — never the code of
    # the browser flow.
    assert len(polls) == 3
    assert all(p["device_code"] == "dev-1234" for p in polls)
    assert len({p["verifier"] for p in polls}) == 1
    assert "code" not in polls[0]

    # The interval of the service is honoured, one wait per pending answer.
    assert slept == [7.0, 7.0]

    out = capsys.readouterr().out
    assert "Open https://example.test/cli and enter the code WXYZ-99." in out
    assert _FAKE_TOKEN not in out
    assert env_file.read_text() == f"FLIGHTPLAN_API_KEY={_FAKE_TOKEN}\n"


def test_headless_expired_stops_with_advice(tmp_path, monkeypatch, capsys):
    env_file = _home(tmp_path, monkeypatch)
    server, url = stub_service({
        "/auth/device/start": lambda body: (200, _STARTED),
        "/auth/device": _sequence((400, {"error": "expired"})),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    try:
        assert login.main(["--headless"], sleep=lambda _s: None) == 1
    finally:
        server.shutdown()

    assert "expired" in capsys.readouterr().out.lower()
    assert not env_file.exists()   # a failed login stores nothing


def test_browser_failure_falls_back_to_headless(tmp_path, monkeypatch, capsys):
    env_file = _home(tmp_path, monkeypatch)

    def explode(_target):
        raise RuntimeError("no display")

    server, url = stub_service({
        "/auth/device/start": lambda body: (200, _STARTED),
        "/auth/device": _sequence((200, {"token": _FAKE_TOKEN, "credential_id": "c2"})),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    monkeypatch.setattr(login.webbrowser, "open", explode)
    try:
        assert login.main([], sleep=lambda _s: None) == 0
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "WXYZ-99" in out                       # the code flow took over
    assert env_file.read_text() == f"FLIGHTPLAN_API_KEY={_FAKE_TOKEN}\n"


def test_browser_that_does_not_open_keeps_waiting(tmp_path, monkeypatch, capsys):
    """`open` returning False is not a failure: the listener is still good, so
    the address is printed and the flow waits for it."""
    env_file = _home(tmp_path, monkeypatch)
    seen: dict = {}
    approve = _browser_that_approves(seen)

    def declines(target):
        approve(target)      # a browser starts late, after the address is shown
        return False

    server, url = stub_service({
        "/auth/device": lambda _b: (200, {"token": _FAKE_TOKEN, "credential_id": "c3"}),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    monkeypatch.setattr(login.webbrowser, "open", declines)
    try:
        assert login.main([]) == 0
    finally:
        server.shutdown()

    assert f"{url}/cli/authorize?" in capsys.readouterr().out
    assert env_file.read_text() == f"FLIGHTPLAN_API_KEY={_FAKE_TOKEN}\n"


# --------------------------------------------------------------------------- #
# URL resolution
# --------------------------------------------------------------------------- #

def test_url_order_is_flag_then_env_then_pin_then_default(tmp_path, monkeypatch):
    (tmp_path / ".flightplan.toml").write_text(
        'repo = "demo"\nurl = "https://pinned.test"\n'
    )
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("FLIGHTPLAN_URL", "https://env.test")
    assert login.resolve_url("https://flag.test/") == "https://flag.test"
    assert login.resolve_url(None) == "https://env.test"

    monkeypatch.delenv("FLIGHTPLAN_URL")
    assert login.resolve_url(None) == "https://pinned.test"

    # With no flag, no environment, and no pin, the public default is left.
    monkeypatch.setattr(login.config, "find_pin", lambda start=None: config.EMPTY)
    assert login.resolve_url(None) == install.DEFAULT_URL


# --------------------------------------------------------------------------- #
# logout
# --------------------------------------------------------------------------- #

def test_logout_removes_the_key_line_only(tmp_path, monkeypatch, capsys):
    env_file = _home(tmp_path, monkeypatch)
    install._write_key_file(_FAKE_TOKEN)
    # A second line, to prove logout takes the key line and nothing else.
    install._write_env_lines(
        env_file, ["FLIGHTPLAN_OTHER=keep", f"FLIGHTPLAN_API_KEY={_FAKE_TOKEN}"],
    )

    assert login.logout_main([]) == 0

    assert env_file.read_text() == "FLIGHTPLAN_OTHER=keep\n"
    assert (env_file.stat().st_mode & 0o777) == 0o600
    out = capsys.readouterr().out
    assert str(env_file) in out
    assert "/devices" in out
    assert _FAKE_TOKEN not in out


def test_logout_without_a_credential_says_so(tmp_path, monkeypatch, capsys):
    env_file = _home(tmp_path, monkeypatch)
    assert login.logout_main([]) == 0
    assert f"No credential is stored in {env_file}." in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Front-door dispatch
# --------------------------------------------------------------------------- #

def test_cli_dispatches_login_with_its_flags(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(login, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["login", "--headless"]) == 0
    assert seen == [["--headless"]]


def test_cli_dispatches_logout(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(login, "logout_main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["logout"]) == 0
    assert seen == [[]]


# --------------------------------------------------------------------------- #
# MCP registration after the login (decisions bcdc4caa and 72315903)
# --------------------------------------------------------------------------- #

def test_register_mcp_runs_the_promptless_registrar(monkeypatch):
    seen: dict = {}

    def registrar(root, *, agent, url, source):
        seen.update(agent=agent, url=url, source=source)
        return True

    monkeypatch.setattr(install, "_register_agents", registrar)
    login.register_mcp("https://example.test")
    assert seen == {
        "agent": "both",
        "url": "https://example.test",
        "source": install.PACKAGE_SOURCE,
    }


def test_register_mcp_forwards_a_custom_source(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        install, "_register_agents",
        lambda root, *, agent, url, source: seen.update(source=source) or True,
    )
    login.register_mcp("https://example.test", source="/src/local")
    assert seen["source"] == "/src/local"


def test_register_mcp_failure_cannot_fail_the_login(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("mcp add exploded")

    monkeypatch.setattr(install, "_register_agents", boom)
    login.register_mcp("https://example.test")
    assert "could not set up the MCP registration" in capsys.readouterr().out


def test_login_source_flag_reaches_registration(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    seen: dict = {}
    server, url = stub_service({
        "/auth/device": lambda body: (200, {"token": _FAKE_TOKEN}),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    monkeypatch.setattr(login.webbrowser, "open", _browser_that_approves({}))
    monkeypatch.setattr(
        login, "register_mcp", lambda u, source: seen.update(source=source),
    )
    monkeypatch.setattr(
        login, "find_repository", lambda u, *, headless, sleep: None,
    )
    try:
        assert login.main(["--source", "/src/local"]) == 0
    finally:
        server.shutdown()
    assert seen["source"] == "/src/local"


def test_login_runs_the_mcp_step_before_the_repository_check(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    order: list[str] = []
    server, url = stub_service({
        "/auth/device": lambda body: (200, {"token": _FAKE_TOKEN}),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    monkeypatch.setattr(login.webbrowser, "open", _browser_that_approves({}))
    monkeypatch.setattr(login, "register_mcp", lambda u, source: order.append("mcp"))
    monkeypatch.setattr(
        login, "find_repository",
        lambda u, *, headless, sleep: order.append("repo"),
    )
    try:
        assert login.main([]) == 0
    finally:
        server.shutdown()
    assert order == ["mcp", "repo"]


def test_failed_login_skips_the_mcp_step(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    server, url = stub_service({
        "/auth/device/start": lambda body: (
            200, {"device_code": "d", "user_code": "u", "interval": 0},
        ),
        "/auth/device": lambda body: (400, {"error": "expired"}),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    monkeypatch.setattr(
        login, "register_mcp",
        lambda *a, **k: pytest.fail("no MCP step after a failed login"),
    )
    try:
        assert login.main(["--headless"], sleep=lambda _s: None) == 1
    finally:
        server.shutdown()
