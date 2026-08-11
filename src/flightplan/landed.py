"""`getflightplan landed` — tell the registry that finished work reached git.

An intent completed with `uncommitted: true` says the work exists nowhere but
someone's working tree. That is the loudest signal the registry has, and it
cannot expire on its own: the service cannot see your tree, so until somebody
says the work landed it keeps warning every agent who touches those paths.
This verb is that correction. It records when the work landed, and which
commits carried it if you pass them.

Explicit SHAs only. The client never guesses which commits belong to an
intent — that guess is the ambiguity that kept a push hook out of the product
(decision d3f98a01), and a wrong SHA is worse than none. Landing with no SHAs
is normal and complete on its own: the timestamp is the correction.

Landing is idempotent and never rewrites the completion record. The outcome,
the reported files, and the original `uncommitted` declaration all stand — the
pair of facts is the history.

Stdlib only, like every other verb here. The heavier imports are deferred into
`main`, so `body_for` costs the MCP server nothing to import.
"""

from __future__ import annotations

import argparse


def body_for(commits: list[str] | None) -> dict:
    """The request body for a landing — the one rule both clients use.

    Omitted and empty are NOT the same statement. No `commits` key means "I do
    not know which commits carried this"; an empty list means "I checked, there
    are none". The MCP tool and this command must say the same thing for the
    same input, so neither invents a report the caller did not make.
    """
    if commits is None:
        return {}
    return {"commits": [sha.strip() for sha in commits if sha.strip()]}


def _clean(value: object) -> str:
    """Text from the service, safe to print on one line.

    A `detail` is written by the service and can quote content people chose —
    an author handle, a repository name. An escape sequence in it could rewrite
    what the user sees on this terminal, so it goes through the same scrubber
    every other service string here does (register.clean).
    """
    from . import register

    return register.clean(value)


def _message(status: int, body: dict) -> str:
    """What to print when the service refuses. Its own words beat anything
    this client could invent — it knows why. Scrubbed before it is printed."""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and _clean(detail).strip():
        return _clean(detail).strip()
    return f"The service refused the request ({status})."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="getflightplan landed",
        description="Record that a completed intent's work is now in git.",
    )
    parser.add_argument("intent_id", help="the intent id you were given at post time")
    parser.add_argument(
        # No default: an absent flag has to stay distinguishable from an empty
        # one, because the two say different things on the wire (body_for).
        "--commit", action="append", default=None, metavar="SHA",
        help="a commit that carried the work; repeat for more. Optional — "
             "landing without SHAs is valid.",
    )
    parser.add_argument("--url", default=None, help="the service to tell")
    args = parser.parse_args(argv)

    from . import install, login, register

    token = install._read_key_file()
    if not token:
        print("No credential found. Run: getflightplan login")
        return 1

    url = login.resolve_url(args.url, install._repo_root())
    endpoint = f"{url}/intents/{args.intent_id}/landed"
    try:
        status, body = register.call("POST", endpoint, token, body_for(args.commit))
    except register.RegisterError as err:
        print(str(err))
        return 1

    if status >= 400:
        print(_message(status, body))
        return 1

    intent = body.get("intent") if isinstance(body, dict) else None
    when = _clean((intent or {}).get("landed_at") or "now")
    print(f"Landed; recorded at {when}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
