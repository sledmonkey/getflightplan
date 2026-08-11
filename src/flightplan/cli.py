"""FlightPlan front door (`getflightplan`) — the one command humans type.

A thin multiplex over the two things the package does, so a single branded
verb covers install and the MCP stdio client:

  - `getflightplan install …` — forwards verbatim to the install kit
    (`install.main`).
  - `getflightplan uninstall …` — forwards verbatim to the removal kit
    (`uninstall.main`).
  - `getflightplan login …` — gets a credential from the service and stores it
    on this machine (`login.main`).
  - `getflightplan logout` — removes that stored credential (`login.logout_main`).
  - `getflightplan register` — finds this repository in the registry, or
    registers it (`register.main`). `login` runs the same check on its own.
  - `getflightplan landed <intent-id>` — records that a completed intent's
    work reached git (`landed.main`), so it stops warning everyone.
  - `getflightplan mcp` — runs the stdio MCP client (`mcp_server.main`), the
    process an agent's MCP config launches.

The import package is `flightplan` too — one name everywhere (decision
f95c02e6); the legacy `intent-registry-*` scripts were dropped in that rename.

Stdlib only, and every heavy import is deferred into the subcommand that needs
it: the uvx client ships only httpx+mcp (pyproject.toml). Keep this file's
top-level imports to the standard library.
"""

from __future__ import annotations

import argparse


def _version() -> str:
    """The installed distribution version, or "unknown" when the metadata is
    absent (e.g. run straight from a source tree that was never installed)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("getflightplan")
    except PackageNotFoundError:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    # A top-level positional `command` plus a REMAINDER tail: everything after
    # the subcommand is forwarded to it verbatim, including leading-dash flags
    # (`getflightplan install --dry-run --agent codex`). REMAINDER *on a
    # subparser* mishandles a leading option — the top-level positional form
    # does not — so we dispatch by hand and let each target parse its own args.
    parser = argparse.ArgumentParser(
        prog="getflightplan",
        description="FlightPlan — shared task memory for coding agents.",
        epilog="commands: install (per-repo setup) · uninstall (per-repo "
        "removal) · login (get a credential) · logout (remove it) · register "
        "(find or register this repository) · landed (mark completed work as "
        "committed) · mcp (stdio client an agent launches).",
    )
    parser.add_argument(
        "--version", action="version", version=f"getflightplan {_version()}",
    )
    parser.add_argument(
        "command", nargs="?",
        choices=["install", "uninstall", "login", "logout", "register", "landed",
                 "mcp"],
        help="what to run",
    )
    parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.command == "install":
        # Lazy import to keep dispatch uniform; install.py is itself stdlib-only.
        from . import install

        return install.main(args.rest)

    if args.command == "uninstall":
        # Lazy for the same reason as install; uninstall.py is stdlib-only too.
        from . import uninstall

        return uninstall.main(args.rest)

    if args.command == "login":
        # Lazy like the others; login.py is stdlib-only too.
        from . import login

        return login.main(args.rest)

    if args.command == "logout":
        from . import login

        return login.logout_main(args.rest)

    if args.command == "register":
        # Lazy like the others; register.py is stdlib-only too.
        from . import register

        return register.main(args.rest)

    if args.command == "landed":
        # Lazy like the others; landed.py is stdlib-only too.
        from . import landed

        return landed.main(args.rest)

    if args.command == "mcp":
        # Lazy: mcp_server imports httpx+mcp (present in the thin uvx env).
        from . import mcp_server

        mcp_server.main()
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
