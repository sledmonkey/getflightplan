#!/usr/bin/env python3
r"""Claude Code Stop hook: don't end a session that still has open intents
(ROADMAP item 9 — un-gated 2026-07-13 after dogfooding showed expired intents,
i.e. outcomes the team lost).

Installed by `getflightplan install` into `.claude/hooks/`, with the matching
Stop entry written into `.claude/settings.json`:

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
        "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/flightplan_stop_hook.py\""}]}]}}

Config chain (all advisory, degrade to allowing the stop):
  - repo: the readable name from the nearest `.flightplan.toml` (cwd, then each
    parent) — `name = "..."`, else the older `repo = "..."` — then git origin
    basename, then cwd name. The pin comes first so every agent on this repo —
    and this hook — ask about the one name intents were posted under. This
    mirrors `flightplan/config.py`; the two must agree. It is duplicated
    because this file runs standalone under whatever `python3` is on PATH.
  - target_id: sent alongside the name when the pin carries one, so the check
    lands on this repo even if the readable name has drifted, and never on a
    different repo that happens to share the name. The name is still sent, for
    a registry that predates the id.
  - subtree: added to the query under a project pin (`target = "project"`),
    which widens the check to the project's child repositories — work demoted
    into one of them is still this session's to close. A registry that predates
    the param ignores it and answers as before.
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

That flag is Claude Code's. Other harnesses run these hooks without it —
Cursor re-fires the hook on every stop with no flag, and a hook that trusted
the flag alone re-blocked forever. The payload itself tells the harnesses
apart: Claude Code sends the `stop_hook_active` key on EVERY stop (false on a
fresh attempt), Cursor never sends it.

So the guard splits on that key. Present → Claude Code: the flag stays the
only guard, because there a LATER stop in the same conversation must nag
again — an agent that renews its intent, works on, and ends hours later
still gets the end-of-session nag this hook exists for. Absent → Cursor
path: each delivered nag is one marker file under the XDG cache dir, named
by the SHA-256 of (session, repo, set of active intent ids), and a stop
that finds a fresh marker passes. Markers are created with exclusive open —
concurrent sessions each keep their own record, where a shared
read-modify-write file lost records under concurrency and re-nagged the
losers. When the hook cannot record a block, it does not block — a nag it
cannot remember is a nag it would repeat forever.

Advisory rule, same as everything else in this system: any failure — missing
config, unreachable registry, bad JSON — allows the stop. This hook must never
trap a user in a session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT = 4  # seconds; a slow registry must not make ending a session slow
BLOCK_MEMORY = 24 * 3600  # seconds a recorded block keeps suppressing repeats


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
        # UTF-8 explicitly: TOML is UTF-8 by specification, and a pinned
        # repository name can hold any text. The locale must not decide it.
        for line in path.read_text(encoding="utf-8").splitlines():
            m = pattern.match(line)
            if m:
                return m.group(1)
    except (OSError, UnicodeDecodeError):
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
    hasn't touched yet.

    Either pin shape works — `name` alongside a pinned id, or the older bare
    `repo` key.
    """
    toml = _find_toml()
    if toml is not None:
        pinned = _toml_value(toml, "name") or _toml_value(toml, "repo")
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


def pinned_target() -> tuple[str | None, str | None]:
    """What the pin points at: (target, target_id). Both None on the older
    name-only pin, and on repos the installer hasn't touched."""
    toml = _find_toml()
    if toml is None:
        return None, None
    return _toml_value(toml, "target"), _toml_value(toml, "target_id")


def active_intents(
    url: str, key: str, repo: str,
    target_id: str | None = None, subtree: bool = False,
) -> list[dict]:
    params = {"repo": repo, "status": "active", "limit": 20}
    if target_id:
        # Sent next to the name, not instead of it: a registry that predates
        # the id ignores it and still filters by name.
        params["target_id"] = target_id
        if subtree:
            # A project id matches only intents posted AT the project. Work
            # that landed in one child repo is stored on that repo's target, so
            # without this the session's own open intent looks like none.
            params["subtree"] = 1
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url.rstrip('/')}/intents?{query}",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())["intents"]


def _state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(base) if base else Path.home() / ".cache"
    return root / "flightplan" / "stop_hook_blocks"


def _block_key(payload: dict, repo: str, intents: list[dict]) -> str:
    """One key per nag: this session, this repo, this exact set of open
    intents. A new intent changes the key, so it earns a fresh nag."""
    session = ""
    for field in ("session_id", "transcript_path", "conversation_id", "thread_id"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            session = value
            break
    if not session:
        # A harness that identifies the session in no way we know degrades to
        # one nag per (repo, intent set) per machine per BLOCK_MEMORY.
        session = str(Path.cwd())
    ids = ",".join(sorted(i.get("id", "") for i in intents))
    return hashlib.sha256(f"{session}\n{repo}\n{ids}".encode()).hexdigest()


def _prune(now: float) -> None:
    """Best-effort: drop markers past BLOCK_MEMORY so the dir stays small.
    A marker another process deletes first, or cannot be deleted, is fine —
    each file stands alone."""
    try:
        for marker in _state_dir().iterdir():
            try:
                if now - marker.stat().st_mtime >= BLOCK_MEMORY:
                    marker.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _should_block(key: str) -> bool:
    """One empty marker file per delivered nag, created with exclusive open.
    No read-modify-write exists, so concurrent sessions cannot lose each
    other's records — the failure mode of the shared JSON file this replaced.
    True = no fresh marker existed and ours is now on disk. Any failure reads
    False: a nag the hook cannot remember is a nag it would repeat forever."""
    marker = _state_dir() / key
    now = time.time()
    try:
        if now - marker.stat().st_mtime < BLOCK_MEMORY:
            return False  # this exact nag was already delivered
        # A stale marker: refresh it and nag once more. Two processes can
        # race here and both nag — rare, bounded, cheaper than a lock.
        os.utime(marker, (now, now))
        _prune(now)
        return True
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        with open(marker, "x"):
            pass
        _prune(now)
        return True
    except FileExistsError:
        return False  # another process delivered this nag just now
    except OSError:
        return False


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
    target, target_id = pinned_target()
    repo = repo_name()
    try:
        intents = active_intents(*cfg, repo, target_id, target == "project")
    except Exception:
        return 0  # advisory: an unreachable registry never traps a session
    if not intents:
        return 0

    # Second loop guard, only when the payload has no stop_hook_active key at
    # all (Cursor): block once per (session, repo, intent set), remembered as
    # one marker file on disk. Claude Code sends the key on every stop and
    # keeps the flag as its only guard — the disk memory would silence the
    # later stops that must still nag. Unrecordable → allow; this hook must
    # never trap a session.
    if "stop_hook_active" not in payload:
        if not _should_block(_block_key(payload, repo, intents)):
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
