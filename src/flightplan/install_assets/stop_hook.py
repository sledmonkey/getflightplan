#!/usr/bin/env python3
r"""Claude Code Stop hook: don't end a session that still has open intents
(ROADMAP item 9 — un-gated 2026-07-13 after dogfooding showed expired intents,
i.e. outcomes the team lost).

Installed by `getflightplan install` into `.claude/hooks/`, with the matching
Stop entry written into `.claude/settings.json`:

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
        "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/flightplan_stop_hook.py\""}]}]}}

Config chain (all advisory, degrade to allowing the stop):
  - repo: `repo = "..."` from the nearest `.flightplan.toml` (cwd, then each
    parent) → git origin basename → cwd name. The pin comes first so every agent
    on this repo — and this hook — ask about the one name intents were posted under.
  - url:  FLIGHTPLAN_URL env → `url = "..."` from that same toml. No url →
    allow the stop.
  - key:  FLIGHTPLAN_API_KEY env → FLIGHTPLAN_API_KEY from
    `~/.config/flightplan/env`. No key → allow the stop.

Mechanics: the registry's session id is minted inside the MCP server process,
so this hook cannot know it. Instead it asks a repo-scoped question — "are
there ANY active intents in this repo?" — and when yes, blocks the stop once
with a reason telling the agent to check `list_intents(session="current")`
(the client-side alias that DOES know the session) and complete its own.
`stop_hook_active` guards the retry loop: the second stop always passes.

Advisory rule, same as everything else in this system: any failure — missing
config, unreachable registry, bad JSON — allows the stop. This hook must never
trap a user in a session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT = 4  # seconds; a slow registry must not make ending a session slow


def _find_toml() -> Path | None:
    """The nearest `.flightplan.toml`, walking cwd upward — where
    `getflightplan install` pins the repo name and url."""
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        candidate = d / ".flightplan.toml"
        if candidate.exists():
            return candidate
    return None


def _toml_value(path: Path, key: str) -> str | None:
    """Pull `key = "value"` out of the pin file with a tolerant regex — no
    tomllib (system python3 may predate it), no dependency."""
    pattern = re.compile(rf'^\s*{re.escape(key)}\s*=\s*"([^"]+)"')
    try:
        for line in path.read_text().splitlines():
            m = pattern.match(line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def config() -> tuple[str, str] | None:
    url = os.environ.get("FLIGHTPLAN_URL", "").strip()
    if not url:
        toml = _find_toml()
        if toml is not None:
            url = _toml_value(toml, "url") or ""
    key = os.environ.get("FLIGHTPLAN_API_KEY", "").strip()
    if not key:
        user_env = Path.home() / ".config" / "flightplan" / "env"
        if user_env.exists():
            for line in user_env.read_text().splitlines():
                if line.startswith("FLIGHTPLAN_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return (url, key) if url and key else None


def repo_name() -> str:
    """The name intents were posted under: the toml pin first, else the old
    derivation (git origin basename, else cwd name) for repos the installer
    hasn't touched yet."""
    toml = _find_toml()
    if toml is not None:
        pinned = _toml_value(toml, "repo")
        if pinned:
            return pinned
    try:
        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=TIMEOUT,
        ).stdout.strip()
        if origin:
            return Path(origin.removesuffix(".git")).name
    except Exception:
        pass
    return Path.cwd().name


def active_intents(url: str, key: str, repo: str) -> list[dict]:
    query = urllib.parse.urlencode({"repo": repo, "status": "active", "limit": 20})
    req = urllib.request.Request(
        f"{url.rstrip('/')}/intents?{query}",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())["intents"]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("stop_hook_active"):
        return 0  # we already blocked once this stop; never loop

    cfg = config()
    if cfg is None:
        return 0
    try:
        intents = active_intents(*cfg, repo_name())
    except Exception:
        return 0  # advisory: an unreachable registry never traps a session
    if not intents:
        return 0

    listing = "; ".join(
        f"{i['id'][:8]} ({i['author']}): {i['summary'][:80]}" for i in intents[:5]
    )
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"The intent registry shows {len(intents)} active intent(s) in this "
            f"repo: {listing}. Check whether any are THIS session's work with "
            "list_intents(session=\"current\") — if so, call complete_intent with "
            "a real outcome paragraph (or update_intent to renew if the work "
            "genuinely continues). If none are yours, end normally; other "
            "sessions' intents are their owners' to close."
        ),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
