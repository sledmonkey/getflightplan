"""Find this repository in FlightPlan, or register it.

The registry stores work under a repository. Before slice 6, a repository was
named by hand in `.flightplan.toml`, so two clones of the same code could post
under two different names and never see each other. This module removes that
step: it asks the service which repository this checkout is, and it pins the
answer.

The check is a lookup, not a guess. The client sends two git facts:

  - the origin remote, raw. The service normalizes it, so `git@host:a/b.git`
    and `https://host/a/b` are one repository.
  - up to 1000 commit shas from HEAD backwards. They prove the clone. A
    stranger who knows the remote URL of a private repository cannot show its
    commits.

Four answers are possible, and each has one short path:

  - your account has access → the pin file gets the id and the name. Done.
  - your request is pending → a notice, because your work is private until an
    owner approves you.
  - the repository is there, but you have no access → the client offers to
    request it.
  - the service knows no such repository → the client offers to register it,
    in the browser, with a code that expires.

`discover()` runs at the end of `getflightplan login`, and on its own as
`getflightplan register`. It never fails the login: the credential is already
stored by then, so a registry problem is one printed line, not an error.

A repository name is chosen by whoever registered the repository, so it is
someone else's text. Every string from the service goes through `clean()`
before it is printed, and anything that reaches the pin file goes through
`pin_safe()` as well. The service also rejects these characters; the client
does not rely on that.

Stdlib only, like the rest of the client (urllib, json, subprocess,
webbrowser). No new dependency (pyproject.toml).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import NamedTuple

from . import config, install, login

# How long one git command may take, in seconds. Every git failure is soft.
GIT_TIMEOUT = 5.0

# How many commit shas the lookup carries. The service caps it at the same
# number, so a longer list would only be cut.
MAX_HEADS = 1000

# How long one HTTP call to the service may take, in seconds.
HTTP_TIMEOUT = login.HTTP_TIMEOUT

# What the pin file records for a registered repository.
PIN_TARGET = "repository"

# Printed when the account has no access yet. Short sentences on purpose: the
# user has to understand what still works and what does not.
PENDING_NOTICE = (
    "Your request is pending. An owner must approve it.\n"
    "Until then, FlightPlan records your work privately, under your personal "
    "account.\n"
    "Shared collision checks and shared context from this repository are not "
    "available.\n"
    "Approval changes your future posts only."
)


class RegisterError(Exception):
    """A step that cannot continue. The caller prints the message."""


# --------------------------------------------------------------------------- #
# Server strings
# --------------------------------------------------------------------------- #

def clean(value: object) -> str:
    """A string from the service, safe to print on one line.

    A repository name is chosen by whoever registered the repository, so it is
    someone else's text arriving on this terminal. An escape sequence in it
    could rewrite what the user sees — including the question they are about
    to answer — and a bidi override could make the name render as something it
    does not say. Both go, along with every other control character.

    The character set is `install._UNSAFE_CHARS`, shared with the pin-file
    writer so the two cannot drift apart. Ordinary non-ASCII text is kept:
    accents and CJK are normal in a repository name.
    """
    return install._UNSAFE_CHARS.sub("", str(value))


def pin_safe(value: object) -> str:
    """`clean`, and also without a double quote or a backslash.

    Anything written to the pin file has to survive two readers. `config.py`
    parses the file with a tolerant line regex rather than a TOML parser, and
    the vendored stop hook carries its own copy of that regex — neither reads
    an escape. So a correctly escaped `\\"` would come back truncated, and the
    two parsers would disagree with `tomllib` about what the name is.

    Dropping both characters costs nothing real (no repository name needs
    them) and it removes the disagreement: what is written contains no escape
    at all, so every reader gets the same string back.
    `install._toml_string` still escapes, as the second line of defence for
    values that come from somewhere other than here.
    """
    return clean(value).replace("\\", "").replace('"', "")


# --------------------------------------------------------------------------- #
# Git facts
# --------------------------------------------------------------------------- #

class GitFacts(NamedTuple):
    """What the lookup needs, plus what stopped us from getting it."""

    remote: str            # the origin URL, raw — the service normalizes it
    heads: tuple[str, ...]  # commit shas, newest first
    shallow: bool          # a shallow clone holds only part of the history
    problem: str           # "" when the facts are usable

    @property
    def head(self) -> str:
        """The sha of HEAD — the first line of the list."""
        return self.heads[0] if self.heads else ""


def _git(root: Path, *args: str) -> str:
    """One git command, or "" for any failure. Git can be missing, this can be
    no repository at all, and the command can hang; none of that may stop a
    login, so every failure reads the same as an empty answer."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except Exception:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def git_facts(root: Path) -> GitFacts:
    """The facts the lookup sends. `problem` is set when there are none."""
    remote = _git(root, "remote", "get-url", "origin")
    if not remote:
        return GitFacts("", (), False, (
            "FlightPlan found no git origin remote here, so it cannot find "
            "this repository."
        ))

    heads = tuple(_git(root, "rev-list", f"--max-count={MAX_HEADS}", "HEAD").split())
    if not heads:
        return GitFacts(remote, (), False, (
            "This repository has no commits yet, so FlightPlan cannot find it."
        ))

    shallow = _git(root, "rev-parse", "--is-shallow-repository") == "true"
    return GitFacts(remote, heads, shallow, "")


def repo_name_hint(root: Path) -> str:
    """A readable name to offer the service — the same rule the installer uses
    for an unpinned repo."""
    return install._derive_repo(root)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def call(
    method: str,
    endpoint: str,
    token: str,
    payload: dict | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> tuple[int, dict]:
    """One authenticated call, returning (status, body).

    A 4xx is a normal answer here — 403 carries the invite-only reason and 429
    carries a rate limit — so only a dead connection raises. The token goes in
    the header and is never printed.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        endpoint, data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, login._json_body(response.read())
    except urllib.error.HTTPError as err:
        return err.code, login._json_body(err.read())
    except (urllib.error.URLError, OSError) as err:
        raise RegisterError(
            f"The service at {endpoint} did not answer ({err})."
        ) from err


def _service_line(status: int, body: dict) -> str:
    """One plain line for an answer the client cannot use."""
    if status == 429:
        return (
            "FlightPlan is busy and refused the request. Wait a minute, then "
            "run `getflightplan register`."
        )
    detail = clean(body.get("detail") or body.get("error") or f"HTTP {status}")
    return f"FlightPlan could not do that ({detail})."


# --------------------------------------------------------------------------- #
# The service calls
# --------------------------------------------------------------------------- #

def lookup(url: str, token: str, facts: GitFacts) -> dict | None:
    """Ask which repository this checkout is. None means the service knows
    none. The heads prove the clone; the service normalizes the remote."""
    status, body = call(
        "POST", f"{url}/repos/lookup", token,
        {"remote": facts.remote, "heads": list(facts.heads)},
    )
    if status != 200:
        raise RegisterError(_service_line(status, body))
    match = body.get("match")
    return match if isinstance(match, dict) else None


def register_start(url: str, token: str, facts: GitFacts, name_hint: str) -> dict:
    """Start a browser registration. Returns the code and the page to open."""
    status, body = call(
        "POST", f"{url}/repos/register/start", token,
        {"remote": facts.remote, "head": facts.head, "name_hint": name_hint},
    )
    if status != 200 or not body.get("url"):
        raise RegisterError(_service_line(status, body))
    return body


def register_poll(url: str, token: str, code: str) -> dict:
    """Ask whether the registration finished."""
    query = urllib.parse.urlencode({"code": code})
    status, body = call("GET", f"{url}/repos/register/poll?{query}", token)
    if status != 200:
        raise RegisterError(_service_line(status, body))
    return body


def request_access(url: str, token: str, repository_id: str) -> tuple[int, dict]:
    """Ask an owner for access. 403 means the repository is invite only."""
    path = urllib.parse.quote(repository_id, safe="")
    return call("POST", f"{url}/repos/{path}/requests", token, {})


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

def _is_tty() -> bool:
    """A terminal is attached, so a question can be asked."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _yes(question: str, ask) -> bool:
    """A yes/no question whose default is yes. Anything else is a no, and so
    is a closed input."""
    try:
        answer = ask(question)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("", "y", "yes")


# --------------------------------------------------------------------------- #
# The pin
# --------------------------------------------------------------------------- #

def _pin_match(root: Path, repository_id: str, name: str) -> None:
    """Record the repository in the pin file, and say so.

    Both values come off the wire, so both are cleaned here as well as at the
    call site: this is the one place that writes them, and `clean` costs
    nothing to repeat. `install.write_pin_target` escapes them for TOML.
    """
    repository_id, name = pin_safe(repository_id), pin_safe(name)
    path = install.write_pin_target(
        root, target=PIN_TARGET, target_id=repository_id, name=name,
    )
    print(f'FlightPlan found the repository "{name}". The pin is written to {path}.')


# --------------------------------------------------------------------------- #
# The routine
# --------------------------------------------------------------------------- #

def discover(
    root: Path,
    url: str,
    token: str,
    interactive: bool = True,
    *,
    headless: bool = False,
    sleep=time.sleep,
    clock=time.monotonic,
    ask=input,
    explain: bool = False,
) -> int:
    """Find this repository in FlightPlan, or offer to register it.

    `explain` prints why the check was skipped. `getflightplan register` sets
    it, because the user asked for the check and deserves an answer; the run
    at the end of a login stays quiet about a repository that needs nothing.

    Returns 0 when there is nothing more to do, 1 when the user still has a
    step left. Login ignores the number — it already succeeded.
    """
    # The project pin is read first: it also carries an id, but it binds a
    # workspace of several repositories, so this checkout is not the thing to
    # register.
    pin = config.find_pin(root)
    if pin.target == "project":
        if explain:
            print(
                "A project pin covers this folder. FlightPlan registers the "
                "repositories under it, not this folder."
            )
        return 0
    if pin.target_id:
        if explain:
            # Cleaned as well: an older client may have written this file.
            print(
                f'This repository is already pinned as "{clean(pin.name)}" '
                f"({clean(pin.target_id)})."
            )
        return 0

    facts = git_facts(root)
    if facts.problem:
        print(facts.problem)
        return 1
    if facts.shallow:
        print(
            "This is a shallow clone. It holds only part of the history, so "
            "FlightPlan can miss a registration that already exists."
        )

    match = lookup(url, token, facts)
    if match is None:
        return _offer_registration(
            root, url, token, facts, interactive,
            headless=headless, sleep=sleep, clock=clock, ask=ask,
        )

    access = match.get("access")
    name = pin_safe(match.get("name") or "") or repo_name_hint(root)
    if access == "granted":
        _pin_match(root, pin_safe(match.get("repository_id", "")), name)
        return 0
    if access == "pending":
        print(PENDING_NOTICE)
        return 0
    return _offer_access(root, url, token, match, name, interactive, ask=ask)


def _offer_access(
    root: Path,
    url: str,
    token: str,
    match: dict,
    name: str,
    interactive: bool,
    *,
    ask,
) -> int:
    """The repository exists and this account cannot see it. Offer to ask.

    The offer goes out whatever the enrollment policy says. An invite-only
    repository answers 403 with its own reason, and that reason is better than
    anything the client could write from the policy name alone.
    """
    if not (interactive and _is_tty()):
        print(
            f'FlightPlan knows the repository "{name}", but this account has '
            "no access. To ask for it, run: getflightplan register"
        )
        return 1
    if not _yes(f"Request access to '{name}'? [Y/n] ", ask):
        print("No request is sent. To ask later, run: getflightplan register")
        return 1

    repository_id = pin_safe(match.get("repository_id", ""))
    status, body = request_access(url, token, repository_id)
    if status == 403:
        detail = clean(body.get("detail") or "")
        print(detail or f'The repository "{name}" is invite only.')
        return 1
    if status != 200:
        print(_service_line(status, body))
        return 1
    if body.get("status") == "granted":
        _pin_match(root, repository_id, name)
        return 0
    print(PENDING_NOTICE)
    return 0


def _offer_registration(
    root: Path,
    url: str,
    token: str,
    facts: GitFacts,
    interactive: bool,
    *,
    headless: bool,
    sleep,
    clock,
    ask,
) -> int:
    """The service knows no such repository. Offer to register it."""
    if not (interactive and _is_tty()):
        print(
            "FlightPlan does not know this repository yet. To register it, "
            "run: getflightplan register"
        )
        return 1
    if not _yes("Register this repository with FlightPlan? [Y/n] ", ask):
        print("Nothing is registered. To register later, run: getflightplan register")
        return 1

    started = register_start(url, token, facts, repo_name_hint(root))
    target = clean(started["url"])
    print(f"Finish the registration here:\n  {target}")
    if not headless:
        # The address is printed either way, so a browser that will not start
        # costs the user nothing.
        try:
            webbrowser.open(target)
        except Exception:
            pass

    return _await_registration(
        root, url, token, started, sleep=sleep, clock=clock,
    )


def _await_registration(
    root: Path, url: str, token: str, started: dict, *, sleep, clock,
) -> int:
    """Poll until the browser finishes the registration, or the code expires.

    The service owns both the interval and the expiry, and the loop keeps its
    own cap as well: a service that answers `pending` forever must not hold
    the terminal.
    """
    interval = float(started.get("interval") or 2)
    deadline = clock() + float(started.get("expires_in") or 600)
    code = str(started.get("code") or "")

    while clock() < deadline:
        sleep(interval)
        body = register_poll(url, token, code)
        state = body.get("status")
        if state == "complete":
            _pin_match(
                root,
                pin_safe(body.get("repository_id", "")),
                pin_safe(body.get("name") or "") or repo_name_hint(root),
            )
            return 0
        if state == "expired":
            break
    print("The code expired. Run `getflightplan register` to try again.")
    return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None, *, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(
        prog="getflightplan register",
        description="Find this repository in FlightPlan, or register it.",
    )
    parser.add_argument("--url", default=None, help="the service to ask")
    parser.add_argument(
        "--headless", action="store_true",
        help="print the address instead of opening a browser (for a remote shell)",
    )
    args = parser.parse_args(argv)

    token = install._read_key_file()
    if not token:
        print("No credential found. Run: getflightplan login")
        return 1

    root = install._repo_root()
    url = login.resolve_url(args.url, root)
    try:
        return discover(
            root, url, token,
            headless=args.headless, sleep=sleep, explain=True,
        )
    except RegisterError as err:
        print(str(err))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
