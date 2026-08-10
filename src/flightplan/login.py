"""`getflightplan login` — get a credential without a copied API key.

Two flows lead to the same result. The default flow uses the browser. The
`--headless` flow uses a code that you type into another device.

Browser flow (decisions 61822208 and b18cf641):

  1. The client makes a PKCE pair. The verifier stays in this process. The
     challenge goes to the service.
  2. The client opens a listener on 127.0.0.1 with an ephemeral port. The
     listener opens before the browser, so the callback cannot arrive early.
  3. The browser goes to `{url}/cli/authorize`. You approve there.
  4. The service sends the browser back to the listener with a one-time code.
  5. The client sends the code and the verifier to `{url}/auth/device`. The
     service answers with the credential.

Headless flow: `POST {url}/auth/device/start` gives a short user code and a
page to open. The client then polls `{url}/auth/device` until you approve.
This flow is also the automatic fallback. The client changes to it when the
listener cannot bind, or when the browser launcher fails.

After the credential is stored, the login finishes the MCP setup and looks
for this repository in the registry (register.py). Install cannot register
the MCP server while the machine has no credential, and the login is the
moment one appears — completing it here is what keeps onboarding at two
commands: install, then login (decision bcdc4caa). Neither step can fail the
login: the credential is already stored. `--no-register` skips the
repository check.

The credential goes to `~/.config/flightplan/env` with mode 600 — the file
that already holds the API key, so the MCP server and the stop hook find it
with no other change. The client never prints the credential. The client never
writes it to `.flightplan.toml` or to any other file in the repository.

Stdlib only, like the rest of the client. No new dependency (pyproject.toml).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import config, install

# The listener answers this path, and nothing else.
CALLBACK_PATH = "/callback"

# How long to wait for the browser approval, in seconds.
CALLBACK_TIMEOUT = 300.0

# How long one HTTP call to the service may take, in seconds.
HTTP_TIMEOUT = 15.0

# The page the browser shows after the callback. Short on purpose: the tab is
# finished, and the CLI has the rest of the story.
_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>FlightPlan</title>"
    b"<p>You can close this tab.</p>"
)


class LoginError(Exception):
    """A login that cannot continue. `main` prints the message and returns 1."""


# --------------------------------------------------------------------------- #
# PKCE and URLs
# --------------------------------------------------------------------------- #

def pkce_pair() -> tuple[str, str]:
    """A new (verifier, challenge) pair.

    The verifier is 86 URL-safe characters, inside the 43-128 range that PKCE
    allows. The challenge is the SHA-256 of the verifier, in base64url, with
    no padding. Only the challenge leaves this process before the approval.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def resolve_url(flag: str | None = None, start: Path | None = None) -> str:
    """The service URL to log in to.

    The order is: the `--url` flag, then FLIGHTPLAN_URL, then the `url` of the
    nearest pin, then the public default. The flag beats the environment here,
    because the flag is the thing the user typed for this one command.
    """
    url = (
        (flag or "").strip()
        or os.environ.get("FLIGHTPLAN_URL", "").strip()
        or (config.find_pin(start).url or "").strip()
        or install.DEFAULT_URL
    )
    return url.rstrip("/")


def authorize_url(url: str, challenge: str, redirect_uri: str, label: str) -> str:
    """The page the browser opens for the approval."""
    query = urllib.parse.urlencode({
        "challenge": challenge,
        "redirect_uri": redirect_uri,
        "label": label,
    })
    return f"{url}/cli/authorize?{query}"


def device_label() -> str:
    """The name shown beside the credential on the /devices page."""
    return socket.gethostname() or "unknown host"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _json_body(raw: bytes) -> dict:
    """The JSON body, or an empty dict. A proxy or a gateway can answer with
    HTML, and that must not raise over the real error."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def post_json(endpoint: str, payload: dict, timeout: float = HTTP_TIMEOUT) -> tuple[int, dict]:
    """POST JSON and return (status, body). A 4xx is a normal answer here: the
    poll loop reads `authorization_pending` out of one."""
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _json_body(response.read())
    except urllib.error.HTTPError as err:
        return err.code, _json_body(err.read())
    except (urllib.error.URLError, OSError) as err:
        raise LoginError(f"The service at {endpoint} did not answer ({err}).") from err


# --------------------------------------------------------------------------- #
# The loopback listener
# --------------------------------------------------------------------------- #

class _CallbackServer(HTTPServer):
    """A one-shot listener for the redirect from the service."""

    code: str = ""
    error: str = ""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Answers the redirect. It reads the code, then shows a short page."""

    def do_GET(self) -> None:  # noqa: N802 — the BaseHTTPRequestHandler name
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == CALLBACK_PATH:
            query = urllib.parse.parse_qs(parts.query)
            self.server.code = (query.get("code") or [""])[0]
            self.server.error = (query.get("error") or [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_DONE_PAGE)))
        self.end_headers()
        self.wfile.write(_DONE_PAGE)

    def log_message(self, *args) -> None:
        """Silence. The CLI owns the output of this command."""


def make_listener() -> _CallbackServer:
    """Bind the callback listener on 127.0.0.1 with an ephemeral port.

    The address is 127.0.0.1 exactly. `localhost` can resolve to ::1 or to
    more than one address, and 0.0.0.0 would take the callback from the whole
    network. Raises OSError when the bind fails; the caller then goes headless.
    """
    return _CallbackServer(("127.0.0.1", 0), _CallbackHandler)


def redirect_uri(server: _CallbackServer) -> str:
    """Where the service sends the browser back to."""
    return f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"


def wait_for_code(
    server: _CallbackServer,
    timeout: float = CALLBACK_TIMEOUT,
    clock=time.monotonic,
) -> str:
    """Serve the callback, then close the listener and return the code.

    The loop exists for one reason: a stray request, such as a probe from the
    browser, must not use up the single answer. Only the callback ends the
    wait. The listener always closes before this returns.
    """
    server.timeout = 1.0  # so a quiet second returns to the deadline check
    deadline = clock() + timeout
    try:
        while clock() < deadline:
            server.handle_request()
            if server.code or server.error:
                break
    finally:
        server.server_close()

    if server.error:
        raise LoginError(f"The service refused the login ({server.error}).")
    if not server.code:
        raise LoginError(
            "No approval arrived in 5 minutes. "
            "Run `getflightplan login --headless` to log in with a code."
        )
    return server.code


# --------------------------------------------------------------------------- #
# The service calls
# --------------------------------------------------------------------------- #

def stored_credential_id() -> str | None:
    """The id inside the stored token, when there is one.

    A login replaces the credential this machine already holds. The id is the
    non-secret part of the token, so sending it discloses nothing. The service
    revokes that credential and records the new one as its replacement."""
    key = install._read_key_file()
    if not key.startswith("flpd_"):
        return None
    hex_part, _, secret = key.removeprefix("flpd_").partition("_")
    if not hex_part or not secret:
        return None
    return f"cred_{hex_part}"


def exchange(url: str, code: str, verifier: str, replaces: str | None = None) -> dict:
    """Trade the one-time code for the credential."""
    payload: dict = {"code": code, "verifier": verifier}
    if replaces:
        payload["replaces"] = replaces
    status, body = post_json(f"{url}/auth/device", payload)
    if status != 200 or not body.get("token"):
        raise LoginError(_service_error(status, body))
    return body


def device_start(url: str, challenge: str, label: str) -> dict:
    """Start the headless flow. Returns the user code and where to type it."""
    status, body = post_json(
        f"{url}/auth/device/start", {"challenge": challenge, "label": label},
    )
    if status != 200 or not body.get("device_code"):
        raise LoginError(_service_error(status, body))
    return body


def poll_for_token(
    url: str,
    device_code: str,
    verifier: str,
    interval: float,
    sleep=time.sleep,
    replaces: str | None = None,
) -> dict:
    """Ask for the credential until the user approves the device code.

    `authorization_pending` means wait one more interval. The service also
    stops the loop: `expired` ends it, and so does any other error. The service
    owns the expiry, so this loop has no deadline of its own.
    """
    payload: dict = {"device_code": device_code, "verifier": verifier}
    if replaces:
        payload["replaces"] = replaces
    while True:
        status, body = post_json(f"{url}/auth/device", payload)
        if status == 200 and body.get("token"):
            return body
        error = body.get("error", "")
        if error != "authorization_pending":
            raise LoginError(_service_error(status, body))
        sleep(interval)


def _service_error(status: int, body: dict) -> str:
    """One plain line for an answer the client cannot use."""
    error = body.get("error", "")
    if error == "expired":
        return "The login expired. Run `getflightplan login` again."
    detail = error or body.get("detail") or f"HTTP {status}"
    return f"The login failed ({detail})."


# --------------------------------------------------------------------------- #
# Storing the credential
# --------------------------------------------------------------------------- #

def store(credential: dict, label: str) -> int:
    """Write the credential to the env file and report where it went.

    The token itself is never printed, and it never goes into a file in the
    repository. `install._write_key_file` is the one writer for that file.
    """
    token = credential.get("token", "")
    if not token:
        raise LoginError("The service returned no token.")
    path = install._write_key_file(token)
    name = credential.get("label") or label
    identifier = credential.get("credential_id", "")
    shown = f'"{name}" ({identifier})' if identifier else f'"{name}"'
    print(f"The credential {shown} is stored in {path}.")
    return 0


# --------------------------------------------------------------------------- #
# The two flows
# --------------------------------------------------------------------------- #

def run_headless(url: str, verifier: str, challenge: str, label: str, sleep=time.sleep) -> int:
    """The code flow: start, print the code, poll until the user approves."""
    started = device_start(url, challenge, label)
    print(
        f"Open {started.get('verification_uri', url)} and enter the code "
        f"{started.get('user_code', '')}."
    )
    interval = float(started.get("interval") or 5)
    credential = poll_for_token(
        url, started["device_code"], verifier, interval, sleep=sleep,
        replaces=stored_credential_id(),
    )
    return store(credential, label)


def run_browser(url: str, verifier: str, challenge: str, label: str, sleep=time.sleep) -> int:
    """The browser flow, with the headless flow as the fallback."""
    try:
        server = make_listener()
    except OSError:
        print("The callback port is not available. FlightPlan uses a code instead.")
        return run_headless(url, verifier, challenge, label, sleep=sleep)

    target = authorize_url(url, challenge, redirect_uri(server), label)
    try:
        opened = webbrowser.open(target)
    except Exception:
        # The launcher itself failed, so the browser flow cannot finish here.
        server.server_close()
        print("The browser did not start. FlightPlan uses a code instead.")
        return run_headless(url, verifier, challenge, label, sleep=sleep)

    if not opened:
        # No browser started, but the listener is good. Wait for a browser on
        # this machine, which the user can start with the printed address.
        print(f"Open this address to approve the login:\n  {target}")
    else:
        print("Approve the login in your browser.")

    code = wait_for_code(server)
    return store(exchange(url, code, verifier, replaces=stored_credential_id()), label)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def register_mcp(url: str, source: str = install.PACKAGE_SOURCE) -> None:
    """Finish the MCP registration, right after the credential is stored.

    Install cannot register while the machine has no credential. The login is
    when one appears, so the registration runs here too — that is what keeps
    onboarding at two commands (decision bcdc4caa). No prompts, credential
    from the env file only (decision 72315903); agents whose binary is absent
    are skipped inside the registrar. Nothing here may fail the login: every
    error becomes one printed line.
    """
    try:
        if install._register_agents(
            install._repo_root(), agent="both", url=url, source=source,
        ):
            print("  →    start a new agent session in this repo to pick up "
                  "the registration.")
    except Exception as err:
        print(f"FlightPlan could not set up the MCP registration ({err}).")


def find_repository(url: str, *, headless: bool, sleep=time.sleep) -> None:
    """Find or register the repository, right after the credential is stored.

    The login has already succeeded by the time this runs, so nothing here may
    fail it: every error becomes one printed line. The token comes back out of
    the env file the store just wrote, which keeps one reader for the
    credential instead of passing the secret along another path.
    """
    try:
        # Lazy: register imports this module, so the import cannot be at the
        # top of it.
        from . import register

        register.discover(
            install._repo_root(), url, install._read_key_file(),
            headless=headless, sleep=sleep,
        )
    except Exception as err:
        print(f"FlightPlan could not check this repository ({err}).")


def main(argv: list[str] | None = None, *, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(
        prog="getflightplan login",
        description="Log in to FlightPlan and store the credential on this machine.",
    )
    parser.add_argument("--url", default=None, help="the service to log in to")
    parser.add_argument(
        "--headless", action="store_true",
        help="log in with a code instead of a browser (for a remote shell)",
    )
    parser.add_argument(
        "--no-register", action="store_true",
        help="do not look for this repository after the login",
    )
    parser.add_argument(
        "--source", default=install.PACKAGE_SOURCE,
        help="what the MCP registration runs (default: the PyPI package; "
        "pass a git URL or a local path for development)",
    )
    args = parser.parse_args(argv)

    url = resolve_url(args.url)
    verifier, challenge = pkce_pair()
    label = device_label()
    flow = run_headless if args.headless else run_browser
    try:
        code = flow(url, verifier, challenge, label, sleep=sleep)
    except LoginError as err:
        print(str(err))
        return 1

    if code == 0:
        register_mcp(url, source=args.source)
        if not args.no_register:
            find_repository(url, headless=args.headless, sleep=sleep)
    return code


def logout_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="getflightplan logout",
        description="Remove the stored FlightPlan credential from this machine.",
    )
    parser.add_argument("--url", default=None, help="the service the credential is for")
    args = parser.parse_args(argv)

    url = resolve_url(args.url)
    path = install._key_config_file()
    if install._remove_key_line():
        print(f"The credential is removed from {path}; "
              f"the {url}/devices page revokes it on the service.")
    else:
        print(f"No credential is stored in {path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
