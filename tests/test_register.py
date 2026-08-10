"""`getflightplan register`, and the same check at the end of a login.

The service is a stub HTTP server, the same idiom test_login.py uses. Git is
not faked: the git-fact tests run `git init` and one commit in a temp folder,
because the shape of `git rev-list` output is the thing under test.

The rules these tests protect: the pin file gains the id without losing
anything else in it, the token never reaches stdout, and a registry problem
never fails a login.
"""

import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from flightplan import cli, install, login, register

_FAKE_TOKEN = "flpd_abc123_secretpart"


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

def stub_service(routes: dict):
    """An HTTP stub for the repos endpoints.

    `routes` maps a path (without the query) to a function of the request body
    — `{}` for a GET — returning (status, response body). Every request lands
    on `server.calls` as (path, body, auth), which is how a test asserts what
    went out, including the bearer header.
    """
    calls: list[tuple[str, dict, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, body: dict) -> None:
            path = urlsplit(self.path).path
            calls.append((self.path, body, self.headers.get("Authorization") or ""))
            status, reply = routes[path](body)
            payload = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self._answer(json.loads(self.rfile.read(length)) if length else {})

        def do_GET(self):
            self._answer({})

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


def _answers(*replies):
    """A route that gives each reply in turn, then repeats the last one."""
    remaining = list(replies)

    def route(_body):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return route


def _asks(*replies):
    """A stand-in for `input` that answers each question in turn."""
    remaining = list(replies)
    asked: list[str] = []

    def ask(question: str) -> str:
        asked.append(question)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    ask.asked = asked
    return ask


def _tty(monkeypatch, attached: bool = True):
    monkeypatch.setattr(register, "_is_tty", lambda: attached)


def _repo(tmp_path, remote="git@github.test:acme/widgets.git", commits=1):
    """A real git repository with a remote and some commits."""
    root = tmp_path / "repo"
    root.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@test",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path / "githome"),
    }
    (tmp_path / "githome").mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, env=env, check=True)
    if remote:
        subprocess.run(
            ["git", "remote", "add", "origin", remote],
            cwd=root, env=env, check=True,
        )
    for n in range(commits):
        (root / f"f{n}.txt").write_text(str(n))
        subprocess.run(["git", "add", "-A"], cwd=root, env=env, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", f"c{n}"], cwd=root, env=env, check=True,
        )
    return root


# A pin file with a comment, a url, and a key the client does not own.
_EXISTING_PIN = (
    "# hand-written header\n"
    'repo = "widgets"\n'
    'url = "https://pinned.test"\n'
    'nickname = "keep me"\n'
)


# --------------------------------------------------------------------------- #
# Git facts
# --------------------------------------------------------------------------- #

def test_git_facts_read_the_remote_and_the_heads(tmp_path):
    root = _repo(tmp_path, commits=3)
    facts = register.git_facts(root)

    assert facts.problem == ""
    assert facts.remote == "git@github.test:acme/widgets.git"
    assert len(facts.heads) == 3
    assert all(len(sha) == 40 for sha in facts.heads)
    assert facts.head == facts.heads[0]      # HEAD is the first line
    assert facts.shallow is False


def test_git_facts_cap_the_head_list(tmp_path, monkeypatch):
    root = _repo(tmp_path, commits=2)
    seen: list[tuple[str, ...]] = []
    real = register._git

    def spy(root_, *args):
        seen.append(args)
        return real(root_, *args)

    monkeypatch.setattr(register, "_git", spy)
    register.git_facts(root)
    assert ("rev-list", "--max-count=1000", "HEAD") in seen


def test_a_repository_without_a_remote_is_skipped(tmp_path, capsys):
    root = _repo(tmp_path, remote=None)
    facts = register.git_facts(root)
    assert facts.problem
    assert "origin remote" in facts.problem


def test_a_folder_that_is_not_a_repository_is_skipped(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    facts = register.git_facts(plain)
    assert facts.problem
    assert facts.heads == ()


def test_a_repository_without_commits_is_skipped(tmp_path):
    root = _repo(tmp_path, commits=0)
    facts = register.git_facts(root)
    assert "no commits" in facts.problem


def test_discover_prints_the_git_problem_and_stops(tmp_path, capsys):
    root = _repo(tmp_path, remote=None)
    assert register.discover(root, "https://unused.test", _FAKE_TOKEN) == 1
    assert "origin remote" in capsys.readouterr().out


def test_a_shallow_clone_warns(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    monkeypatch.setattr(
        register, "git_facts",
        lambda _root: register.GitFacts("git@x:a/b.git", ("sha1",), True, ""),
    )
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_1", "name": "widgets",
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()
    assert "shallow clone" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The lookup call
# --------------------------------------------------------------------------- #

def test_lookup_sends_the_raw_remote_and_the_heads_with_a_bearer(tmp_path, capsys):
    root = _repo(tmp_path, commits=2)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "widgets",
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()

    (path, body, auth), = server.calls
    assert path == "/repos/lookup"
    assert body["remote"] == "git@github.test:acme/widgets.git"   # raw, not normalized
    assert len(body["heads"]) == 2
    assert auth == f"Bearer {_FAKE_TOKEN}"
    assert _FAKE_TOKEN not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# access: granted
# --------------------------------------------------------------------------- #

def test_granted_writes_the_pin_and_keeps_the_rest_of_the_file(tmp_path, capsys):
    root = _repo(tmp_path)
    pin = root / ".flightplan.toml"
    pin.write_text(_EXISTING_PIN)

    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "acme widgets",
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()

    text = pin.read_text(encoding="utf-8")
    assert 'target = "repository"' in text
    assert 'target_id = "repo_9f3c"' in text
    assert 'name = "acme widgets"' in text
    # Everything the client does not own survives, and the legacy name key is
    # replaced rather than left to disagree with `name`.
    assert "# hand-written header" in text
    assert 'url = "https://pinned.test"' in text
    assert 'nickname = "keep me"' in text
    assert "repo = " not in text

    out = capsys.readouterr().out
    assert "acme widgets" in out and str(pin) in out


def test_granted_writes_a_pin_file_that_was_not_there(tmp_path):
    root = _repo(tmp_path)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_new", "name": "widgets",
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()

    from flightplan import config

    text = (root / ".flightplan.toml").read_text(encoding="utf-8")
    parsed = config.read_pin(text)
    assert (parsed.target, parsed.target_id, parsed.name) == (
        "repository", "repo_new", "widgets",
    )


# --------------------------------------------------------------------------- #
# access: pending
# --------------------------------------------------------------------------- #

def test_pending_prints_the_notice_and_writes_no_pin(tmp_path, capsys):
    root = _repo(tmp_path)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "widgets",
            "enrollment_policy": "request", "access": "pending",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "Your request is pending." in out
    assert "privately" in out
    assert "future posts only" in out
    assert not (root / ".flightplan.toml").exists()


# --------------------------------------------------------------------------- #
# access: none
# --------------------------------------------------------------------------- #

def test_none_then_yes_posts_the_request_and_prints_the_notice(
    tmp_path, monkeypatch, capsys,
):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    ask = _asks("")   # the empty answer takes the default, which is yes
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "widgets",
            "enrollment_policy": "request", "access": "none",
        }}),
        "/repos/repo_9f3c/requests": lambda _b: (200, {"status": "pending"}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN, ask=ask) == 0
    finally:
        server.shutdown()

    assert ask.asked == ["Request access to 'widgets'? [Y/n] "]
    assert [c[0] for c in server.calls] == ["/repos/lookup", "/repos/repo_9f3c/requests"]
    assert "Your request is pending." in capsys.readouterr().out


def test_none_then_no_sends_nothing(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "widgets",
            "enrollment_policy": "request", "access": "none",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN, ask=_asks("n")) == 1
    finally:
        server.shutdown()

    assert [c[0] for c in server.calls] == ["/repos/lookup"]
    assert "getflightplan register" in capsys.readouterr().out


def test_invite_only_prints_the_reason_of_the_service(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "widgets",
            "enrollment_policy": "invite_only", "access": "none",
        }}),
        "/repos/repo_9f3c/requests": lambda _b: (
            403, {"detail": "This repository is invite only. Ask an owner."}
        ),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN, ask=_asks("y")) == 1
    finally:
        server.shutdown()

    assert "This repository is invite only. Ask an owner." in capsys.readouterr().out
    assert not (root / ".flightplan.toml").exists()


def test_a_granted_request_writes_the_pin(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_open", "name": "widgets",
            "enrollment_policy": "request", "access": "none",
        }}),
        "/repos/repo_open/requests": lambda _b: (200, {"status": "granted"}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN, ask=_asks("y")) == 0
    finally:
        server.shutdown()

    assert 'target_id = "repo_open"' in (root / ".flightplan.toml").read_text(encoding="utf-8")


def test_a_rate_limited_request_says_to_wait(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "widgets",
            "enrollment_policy": "request", "access": "none",
        }}),
        "/repos/repo_9f3c/requests": lambda _b: (429, {"detail": "too many"}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN, ask=_asks("y")) == 1
    finally:
        server.shutdown()

    assert "Wait a minute" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# match: null — the registration flow
# --------------------------------------------------------------------------- #

def test_no_match_registers_through_the_browser(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(register.webbrowser, "open", lambda t: opened.append(t) or True)
    slept: list[float] = []

    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": None}),
        "/repos/register/start": lambda _b: (200, {
            "code": "REG-123",
            "url": "https://service.test/register/REG-123",
            "expires_in": 600,
            "interval": 2,
        }),
        "/repos/register/poll": _answers(
            (200, {"status": "pending"}),
            (200, {
                "status": "complete",
                "repository_id": "repo_fresh",
                "name": "widgets",
            }),
        ),
    })
    try:
        assert register.discover(
            root, url, _FAKE_TOKEN, ask=_asks(""), sleep=slept.append,
        ) == 0
    finally:
        server.shutdown()

    # The start call carries the remote, the HEAD sha, and a readable hint.
    start = next(b for p, b, _a in server.calls if p == "/repos/register/start")
    assert start["remote"] == "git@github.test:acme/widgets.git"
    assert len(start["head"]) == 40
    assert start["name_hint"] == "widgets"

    # The poll carries the code, and waits the interval the service gave.
    polls = [p for p, _b, _a in server.calls if p.startswith("/repos/register/poll")]
    assert len(polls) == 2
    assert parse_qs(urlsplit(polls[0]).query)["code"] == ["REG-123"]
    assert slept == [2.0, 2.0]

    # The browser is opened, and the address is printed as well.
    assert opened == ["https://service.test/register/REG-123"]
    out = capsys.readouterr().out
    assert "https://service.test/register/REG-123" in out

    assert 'target_id = "repo_fresh"' in (root / ".flightplan.toml").read_text(encoding="utf-8")


def test_headless_registration_prints_the_address_only(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    monkeypatch.setattr(
        register.webbrowser, "open", lambda _t: pytest.fail("no browser"),
    )
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": None}),
        "/repos/register/start": lambda _b: (200, {
            "code": "REG-9", "url": "https://service.test/register/REG-9",
            "expires_in": 600, "interval": 2,
        }),
        "/repos/register/poll": _answers(
            (200, {"status": "complete", "repository_id": "r1", "name": "widgets"}),
        ),
    })
    try:
        assert register.discover(
            root, url, _FAKE_TOKEN, ask=_asks(""), headless=True,
            sleep=lambda _s: None,
        ) == 0
    finally:
        server.shutdown()


def test_a_browser_that_cannot_start_still_registers(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch)

    def explode(_target):
        raise RuntimeError("no display")

    monkeypatch.setattr(register.webbrowser, "open", explode)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": None}),
        "/repos/register/start": lambda _b: (200, {
            "code": "REG-8", "url": "https://service.test/register/REG-8",
            "expires_in": 600, "interval": 2,
        }),
        "/repos/register/poll": _answers(
            (200, {"status": "complete", "repository_id": "r2", "name": "widgets"}),
        ),
    })
    try:
        assert register.discover(
            root, url, _FAKE_TOKEN, ask=_asks(""), sleep=lambda _s: None,
        ) == 0
    finally:
        server.shutdown()

    assert "https://service.test/register/REG-8" in capsys.readouterr().out


def test_an_expired_code_advises_the_register_command(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    monkeypatch.setattr(register.webbrowser, "open", lambda _t: True)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": None}),
        "/repos/register/start": lambda _b: (200, {
            "code": "REG-7", "url": "https://service.test/register/REG-7",
            "expires_in": 600, "interval": 2,
        }),
        "/repos/register/poll": _answers((200, {"status": "expired"})),
    })
    try:
        assert register.discover(
            root, url, _FAKE_TOKEN, ask=_asks(""), sleep=lambda _s: None,
        ) == 1
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "The code expired. Run `getflightplan register` to try again." in out
    assert not (root / ".flightplan.toml").exists()


def test_the_poll_loop_stops_at_the_expiry_of_the_service(tmp_path, monkeypatch, capsys):
    """A service that answers `pending` forever must not hold the terminal."""
    root = _repo(tmp_path)
    _tty(monkeypatch)
    monkeypatch.setattr(register.webbrowser, "open", lambda _t: True)
    ticks = iter([0.0, 0.0, 5.0, 99.0])
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": None}),
        "/repos/register/start": lambda _b: (200, {
            "code": "REG-6", "url": "https://service.test/register/REG-6",
            "expires_in": 10, "interval": 2,
        }),
        "/repos/register/poll": _answers((200, {"status": "pending"})),
    })
    try:
        assert register.discover(
            root, url, _FAKE_TOKEN, ask=_asks(""), sleep=lambda _s: None,
            clock=lambda: next(ticks),
        ) == 1
    finally:
        server.shutdown()

    assert "expired" in capsys.readouterr().out


def test_no_match_then_no_registers_nothing(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    server, url = stub_service({"/repos/lookup": lambda _b: (200, {"match": None})})
    try:
        assert register.discover(root, url, _FAKE_TOKEN, ask=_asks("no")) == 1
    finally:
        server.shutdown()

    assert [c[0] for c in server.calls] == ["/repos/lookup"]
    assert not (root / ".flightplan.toml").exists()


# --------------------------------------------------------------------------- #
# Skip conditions
# --------------------------------------------------------------------------- #

def test_an_already_pinned_id_skips_the_check(tmp_path, capsys):
    root = _repo(tmp_path)
    (root / ".flightplan.toml").write_text(
        'target = "repository"\ntarget_id = "repo_old"\nname = "widgets"\n'
    )
    # No stub service: reaching the network at all would fail this test.
    assert register.discover(root, "http://127.0.0.1:1", _FAKE_TOKEN) == 0
    assert capsys.readouterr().out == ""


def test_the_skip_reason_is_printed_for_the_register_command(tmp_path, capsys):
    root = _repo(tmp_path)
    (root / ".flightplan.toml").write_text(
        'target = "repository"\ntarget_id = "repo_old"\nname = "widgets"\n'
    )
    assert register.discover(
        root, "http://127.0.0.1:1", _FAKE_TOKEN, explain=True,
    ) == 0
    out = capsys.readouterr().out
    assert "already pinned" in out and "repo_old" in out


def test_a_project_pin_skips_the_check(tmp_path, capsys):
    root = _repo(tmp_path)
    (root / ".flightplan.toml").write_text(
        'target = "project"\ntarget_id = "proj_5b71"\nname = "workspace"\n'
    )
    assert register.discover(
        root, "http://127.0.0.1:1", _FAKE_TOKEN, explain=True,
    ) == 0
    assert "project pin" in capsys.readouterr().out


def test_a_project_pin_above_the_repository_skips_the_check(tmp_path):
    """The project pin sits on the workspace folder, not on the repository."""
    root = _repo(tmp_path)
    (tmp_path / ".flightplan.toml").write_text(
        'target = "project"\ntarget_id = "proj_5b71"\nname = "workspace"\n'
    )
    assert register.discover(root, "http://127.0.0.1:1", _FAKE_TOKEN) == 0


def test_no_register_skips_the_check_after_a_login(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        login, "find_repository", lambda *a, **k: pytest.fail("must not run"),
    )
    server, url = stub_service({
        "/auth/device/start": lambda _b: (200, {
            "device_code": "d1", "user_code": "AB-12",
            "verification_uri": "https://example.test/cli", "interval": 1,
        }),
        "/auth/device": lambda _b: (200, {"token": _FAKE_TOKEN, "credential_id": "c1"}),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    try:
        assert login.main(["--headless", "--no-register"], sleep=lambda _s: None) == 0
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Non-interactive
# --------------------------------------------------------------------------- #

def test_without_a_terminal_the_command_to_run_is_printed(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch, attached=False)
    server, url = stub_service({"/repos/lookup": lambda _b: (200, {"match": None})})
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 1
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "getflightplan register" in out
    assert [c[0] for c in server.calls] == ["/repos/lookup"]


def test_interactive_false_asks_nothing(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch, attached=True)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "widgets",
            "enrollment_policy": "request", "access": "none",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN, False) == 1
    finally:
        server.shutdown()

    assert [c[0] for c in server.calls] == ["/repos/lookup"]
    assert "getflightplan register" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The login hook
# --------------------------------------------------------------------------- #

def test_a_login_runs_the_check_with_the_fresh_credential(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    root = _repo(tmp_path)
    monkeypatch.setattr(install, "_repo_root", lambda: root)
    _tty(monkeypatch, attached=False)

    server, url = stub_service({
        "/auth/device/start": lambda _b: (200, {
            "device_code": "d1", "user_code": "AB-12",
            "verification_uri": "https://example.test/cli", "interval": 1,
        }),
        "/auth/device": lambda _b: (200, {"token": _FAKE_TOKEN, "credential_id": "c1"}),
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_login", "name": "widgets",
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    try:
        assert login.main(["--headless"], sleep=lambda _s: None) == 0
    finally:
        server.shutdown()

    # The lookup went out with the credential the login just stored.
    lookup_call = next(c for c in server.calls if c[0] == "/repos/lookup")
    assert lookup_call[2] == f"Bearer {_FAKE_TOKEN}"
    assert 'target_id = "repo_login"' in (root / ".flightplan.toml").read_text(encoding="utf-8")


def test_a_failed_check_does_not_fail_the_login(tmp_path, monkeypatch, capsys):
    env_file = _home(tmp_path, monkeypatch)

    def explode(*_a, **_k):
        raise RuntimeError("the registry is down")

    monkeypatch.setattr(register, "discover", explode)
    server, url = stub_service({
        "/auth/device/start": lambda _b: (200, {
            "device_code": "d1", "user_code": "AB-12",
            "verification_uri": "https://example.test/cli", "interval": 1,
        }),
        "/auth/device": lambda _b: (200, {"token": _FAKE_TOKEN, "credential_id": "c1"}),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    try:
        assert login.main(["--headless"], sleep=lambda _s: None) == 0
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "the registry is down" in out
    assert env_file.read_text(encoding="utf-8") == f"FLIGHTPLAN_API_KEY={_FAKE_TOKEN}\n"
    assert _FAKE_TOKEN not in out


def test_a_failed_login_runs_no_check(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        login, "find_repository", lambda *a, **k: pytest.fail("must not run"),
    )
    server, url = stub_service({
        "/auth/device/start": lambda _b: (200, {
            "device_code": "d1", "user_code": "AB-12",
            "verification_uri": "https://example.test/cli", "interval": 1,
        }),
        "/auth/device": lambda _b: (400, {"error": "expired"}),
    })
    monkeypatch.setenv("FLIGHTPLAN_URL", url)
    try:
        assert login.main(["--headless"], sleep=lambda _s: None) == 1
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# The register command
# --------------------------------------------------------------------------- #

def test_the_command_needs_a_credential(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    assert register.main([]) == 1
    assert "No credential found. Run: getflightplan login" in capsys.readouterr().out


def test_the_command_uses_the_stored_credential_and_the_url_order(
    tmp_path, monkeypatch, capsys,
):
    _home(tmp_path, monkeypatch)
    install._write_key_file(_FAKE_TOKEN)
    root = _repo(tmp_path)
    (root / ".flightplan.toml").write_text('repo = "widgets"\nurl = "https://pin.test"\n')
    monkeypatch.setattr(install, "_repo_root", lambda: root)
    monkeypatch.chdir(root)

    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_cmd", "name": "widgets",
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        # The flag beats the pin, which is the order login.resolve_url sets.
        assert register.main(["--url", url]) == 0
    finally:
        server.shutdown()

    assert server.calls[0][2] == f"Bearer {_FAKE_TOKEN}"
    assert 'target_id = "repo_cmd"' in (root / ".flightplan.toml").read_text(encoding="utf-8")
    assert _FAKE_TOKEN not in capsys.readouterr().out


def test_the_command_reports_a_service_that_does_not_answer(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    install._write_key_file(_FAKE_TOKEN)
    root = _repo(tmp_path)
    monkeypatch.setattr(install, "_repo_root", lambda: root)

    assert register.main(["--url", "http://127.0.0.1:1"]) == 1
    assert "did not answer" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Hostile server strings
# --------------------------------------------------------------------------- #
#
# A repository name is chosen by whoever registered the repository, so it is
# someone else's text arriving on this terminal and in this repo's pin file.

# A name that tries to close the TOML string and add a key of its own.
_INJECTING_NAME = 'evil"\nurl = "https://attacker.test'


def test_a_hostile_name_cannot_inject_a_key_into_the_pin(tmp_path, monkeypatch):
    import tomllib

    root = _repo(tmp_path)
    pin = root / ".flightplan.toml"
    pin.write_text('repo = "widgets"\nurl = "https://real.test"\n')

    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": _INJECTING_NAME,
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()

    from flightplan import config

    text = pin.read_text(encoding="utf-8")

    # Only one key per line survived: no second `url` was injected. The file
    # still parses, as real TOML and with this repo's own tolerant parser, and
    # the two agree.
    parsed = tomllib.loads(text)
    assert parsed["url"] == "https://real.test"
    assert parsed["target_id"] == "repo_9f3c"
    assert config.read_pin(text).url == "https://real.test"
    assert config.read_pin(text).target_id == "repo_9f3c"
    assert config.read_pin(text).name == parsed["name"]

    # The newline ended the injection, and the quote that would have closed
    # the string early is gone, so the whole payload stays inside the value.
    assert parsed["name"] == 'evilurl = https://attacker.test'
    assert len([ln for ln in text.splitlines() if ln.startswith("url = ")]) == 1


def test_a_registered_name_survives_an_installer_rewrite(tmp_path):
    """`install.run` regenerates the pin from what it reads back, so a name
    written by the registration flow has to survive that round trip exactly."""
    import tomllib

    from flightplan import config

    root = tmp_path / "repo"
    root.mkdir()
    name = register.pin_safe(_INJECTING_NAME)
    install.write_pin_target(
        root, target="repository", target_id="repo_9f3c", name=name,
    )

    install.run(root, agent="claude", repo=None, url=None, dry_run=False)

    text = (root / ".flightplan.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["name"] == name
    assert config.read_pin(text).name == name
    assert config.read_pin(text).target_id == "repo_9f3c"


def test_pin_safe_drops_the_characters_the_tolerant_parser_cannot_read(tmp_path):
    """A quote or a backslash is escaped correctly by `_toml_string`, but
    `config.py` parses the pin with a regex that reads no escape — so the
    registration flow drops both instead of relying on the escape."""
    from flightplan import config

    assert register.pin_safe('a"b') == "ab"
    assert register.pin_safe("a\\b") == "ab"
    assert register.pin_safe("acme widgets — v2") == "acme widgets — v2"

    root = tmp_path / "repo"
    root.mkdir()
    install.write_pin_target(
        root, target="repository", target_id="r1",
        name=register.pin_safe(r'back\slash and "quotes"'),
    )
    text = (root / ".flightplan.toml").read_text(encoding="utf-8")
    assert config.read_pin(text).name == "backslash and quotes"


def test_terminal_escapes_are_stripped_before_a_name_is_printed(
    tmp_path, monkeypatch, capsys,
):
    root = _repo(tmp_path)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c",
            "name": "widgets\x1b[2K\x1b[1Gapproved!",
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "widgets[2K[1Gapproved!" in out
    assert "\x1b" not in (root / ".flightplan.toml").read_text(encoding="utf-8")


def test_terminal_escapes_are_stripped_from_a_403_detail(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "widgets",
            "enrollment_policy": "invite_only", "access": "none",
        }}),
        "/repos/repo_9f3c/requests": lambda _b: (
            403, {"detail": "invite only\x1b[1G\rAccess granted."}
        ),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN, ask=_asks("y")) == 1
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out


def test_terminal_escapes_are_stripped_from_the_registration_address(
    tmp_path, monkeypatch, capsys,
):
    root = _repo(tmp_path)
    _tty(monkeypatch)
    monkeypatch.setattr(register.webbrowser, "open", lambda _t: True)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": None}),
        "/repos/register/start": lambda _b: (200, {
            "code": "REG-5", "url": "https://service.test/r\x1b[2K/REG-5",
            "expires_in": 600, "interval": 2,
        }),
        "/repos/register/poll": _answers(
            (200, {
                "status": "complete", "repository_id": "r3",
                "name": "widgets\x07",
            }),
        ),
    })
    try:
        assert register.discover(
            root, url, _FAKE_TOKEN, ask=_asks(""), sleep=lambda _s: None,
        ) == 0
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert "\x07" not in (root / ".flightplan.toml").read_text(encoding="utf-8")


def test_clean_keeps_ordinary_text(tmp_path):
    assert register.clean("acme widgets — v2 (café)") == "acme widgets — v2 (café)"
    assert register.clean("日本語のリポジトリ 🚀") == "日本語のリポジトリ 🚀"
    assert register.clean("tabs\tand\nnewlines") == "tabsandnewlines"
    assert register.clean("del\x7fhere") == "delhere"


def test_clean_drops_c1_controls_and_bidi_overrides():
    # Hostile characters are written as escapes: a literal invisible
    # character in test source hides what the test actually does.
    assert register.clean("widgets\u009b2Kgone") == "widgets2Kgone"  # CSI
    assert register.clean("widgets\u202estluover") == "widgetsstluover"
    assert register.clean("widgets\u2069\u2066x") == "widgetsx"  # isolates
    assert register.clean("a\u061cb\u200ec\u200fd") == "abcd"  # bidi marks


def test_a_c1_control_in_a_name_is_stripped_before_print_and_write(
    tmp_path, capsys,
):
    root = _repo(tmp_path)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c",
            "name": "widgets\u009b2K\u009b1Gapproved!",  # U+009B twice
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    text = (root / ".flightplan.toml").read_text(encoding="utf-8")
    assert "\u009b" not in out and "\u009b" not in text
    assert "widgets2K1Gapproved!" in out


def test_a_bidi_override_in_a_name_is_stripped_before_print_and_write(
    tmp_path, capsys,
):
    root = _repo(tmp_path)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c",
            # A right-to-left override makes the tail render reversed, so a
            # name can read as one thing and say another.
            "name": "widgets-\u202egnp.exe",
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()

    out = capsys.readouterr().out
    text = (root / ".flightplan.toml").read_text(encoding="utf-8")
    assert "\u202e" not in out and "\u202e" not in text
    assert "widgets-gnp.exe" in out


def test_ordinary_non_ascii_reaches_the_pin_file_unchanged(tmp_path):
    import tomllib

    root = _repo(tmp_path)
    server, url = stub_service({
        "/repos/lookup": lambda _b: (200, {"match": {
            "repository_id": "repo_9f3c", "name": "café — 日本語 🚀",
            "enrollment_policy": "request", "access": "granted",
        }}),
    })
    try:
        assert register.discover(root, url, _FAKE_TOKEN) == 0
    finally:
        server.shutdown()

    # Read as UTF-8, the encoding the pin file is written in — TOML says so,
    # and the locale must not get a vote.
    text = (root / ".flightplan.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["name"] == "café — 日本語 🚀"

    # The client's own reader agrees, whatever the locale is.
    from flightplan import config

    assert config.find_pin(root).name == "café — 日本語 🚀"


def test_cli_dispatches_register_with_its_flags(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(register, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["register", "--headless"]) == 0
    assert seen == [["--headless"]]
