"""Client install kit (`getflightplan install`, ROADMAP 23).

One idempotent command, shipped in the same uvx-distributed package as the MCP
client, that installs/updates every per-repo artifact the registry needs:

  - `.flightplan.toml` — pins what every agent posts under (fixes the observed
    drift where an agent posted under "Code" because it derived the name from a
    parent directory) plus the registry url. Committed on purpose. Two shapes,
    both read by `config.py`: a `repo` name, or a `target_id` with a readable
    `name`. A pinned id is preserved verbatim across runs, never invented here.
  - the agent snippet, dropped into `CLAUDE.md` (Claude Code) and/or `AGENTS.md`
    (Codex) between managed markers, with the repo name pinned into it.
  - the `/registry-digest` command and the session-end stop hook (Claude Code),
    including the `.claude/settings.json` Stop wiring.

Then it verifies MCP registration and registry reachability, printing guidance
for anything missing. Advisory like the rest of the system: verification never
fails the run (it prints warnings); only a genuine write error may raise.

Stdlib only — this ships with the thin uvx client, whose deps (pyproject.toml)
cover nothing more. Assets are loaded via `importlib.resources` so they resolve
from an installed wheel, not just the source tree.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.resources as resources
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import NamedTuple

from . import config

# The service's public default. An existing pin or --url flag overrides it
# (precedence in `_resolve`).
DEFAULT_URL = "https://api.getflightplan.com"

# What generated registrations and guidance lines run. The package is on PyPI,
# so the bare name is enough and `uvx --from` is redundant (ROADMAP 36). The git
# URL still works and is what you pass to `--source` to pin a branch or commit.
PACKAGE_SOURCE = "getflightplan"


def _uvx_argv(source: str) -> list[str]:
    """The argv that starts the MCP server from `source`. `--from` is only
    needed when the source is not the published package name."""
    if source == PACKAGE_SOURCE:
        return ["uvx", "getflightplan", "mcp"]
    return ["uvx", "--from", source, "getflightplan", "mcp"]


def _uvx_command(source: str) -> str:
    """`_uvx_argv` as one line, for guidance text."""
    return " ".join(_uvx_argv(source))


# Managed-block markers. Matched by prefix (line startswith), so the trailing
# text of the begin marker can change without orphaning old blocks. The legacy
# intent-registry markers are still recognized (and rewritten on the next run)
# so pre-rename installs update in place instead of gaining a duplicate block.
BEGIN_MARKER = (
    "<!-- flightplan:begin — managed by getflightplan install; "
    "edits inside this block are overwritten -->"
)
END_MARKER = "<!-- flightplan:end -->"
_LEGACY_BEGIN_PREFIX = "<!-- intent-registry:begin"
_LEGACY_END_PREFIX = "<!-- intent-registry:end"

# The stop-hook command written into .claude/settings.json. $CLAUDE_PROJECT_DIR
# is expanded by Claude Code, so the wiring is location-independent.
STOP_HOOK_REL = ".claude/hooks/flightplan_stop_hook.py"
STOP_HOOK_COMMAND = f'python3 "$CLAUDE_PROJECT_DIR/{STOP_HOOK_REL}"'
# Any Stop command ending in one of these is legacy wiring — drop it: the
# original scripts/ dogfood hook, and the pre-rename vendored hook (whose file
# is also removed in run()).
LEGACY_HOOK_SUFFIXES = ("scripts/stop_hook.py", "hooks/intent_registry_stop_hook.py")
LEGACY_HOOK_REL = ".claude/hooks/intent_registry_stop_hook.py"
# The pre-rename pin file: read as a resolution fallback, removed once the
# renamed pin is written.
LEGACY_PIN_REL = ".intent-registry.toml"

# The manual (unpinned) repo clause — the README/template variant.
_MANUAL_CLAUSE = (
    "use the basename of the git origin remote (or the repo root directory "
    "name if there is no remote)"
)


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #

def _asset_text(name: str) -> str:
    """Load a packaged asset from install_assets/ (works from an installed
    wheel, not only the source checkout)."""
    return (
        resources.files("flightplan") / "install_assets" / name
    ).read_text(encoding="utf-8")


def render_snippet(repo: str | None) -> str:
    """Fill the snippet template's one repo-name slot and return the result.

    `repo=None` renders the manual-derivation clause (the canonical template, as
    it appears in the README); a name renders the pinned clause. Plain
    `.replace`, never `str.format` — the snippet is full of literal braces
    (`kind: "decision"` etc.), so `format` would blow up.
    """
    clause = _MANUAL_CLAUSE if repo is None else (
        f"use `{repo}` (pinned in `.flightplan.toml`; do not derive it)"
    )
    return _asset_text("snippet.md").replace("{repo_clause}", clause)


def _snippet_block(repo: str | None) -> str:
    """The rendered snippet wrapped in managed markers, newline-terminated."""
    return f"{BEGIN_MARKER}\n{render_snippet(repo)}{END_MARKER}\n"


# --------------------------------------------------------------------------- #
# Name / url resolution
# --------------------------------------------------------------------------- #

def _derive_repo(root: Path) -> str:
    """Git origin basename, else the root directory name — the same rule agents
    used before the pin existed."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        origin = out.stdout.strip()
        if origin:
            return Path(origin.removesuffix(".git")).name
    except Exception:
        pass
    return root.name


class Resolved(NamedTuple):
    """What the pin file should say after this run."""
    name: str
    url: str
    target: str | None
    target_id: str | None


def _resolve(root: Path, repo: str | None, url: str | None) -> Resolved:
    """Resolve what to pin, honouring the precedence in the spec.

    Name: `--repo` flag → existing pin (never silently change one) → derive.
    Url:  `--url` flag → existing pin → FLIGHTPLAN_URL env → the public default.
    Id:   whatever is already pinned, verbatim. The installer never mints,
          changes, or drops an id — only the flow that pins one may do that.

    A pre-rename repo has its pin in `.intent-registry.toml`; honour it as the
    existing pin so an upgrade migrates the name/url instead of re-deriving.
    """
    toml_path = root / config.PIN_FILENAME
    legacy_path = root / LEGACY_PIN_REL
    if toml_path.exists():
        existing = toml_path.read_text()
    elif legacy_path.exists():
        existing = legacy_path.read_text()
    else:
        existing = ""
    pin = config.read_pin(existing)
    name = repo or pin.name or _derive_repo(root)
    resolved_url = (
        url
        or pin.url
        or os.environ.get("FLIGHTPLAN_URL", "").strip()
        or DEFAULT_URL
    )
    return Resolved(name, resolved_url, pin.target, pin.target_id)


# The header on the generated pin file — one text for both shapes.
_PIN_HEADER = (
    "# Managed by getflightplan install. Committed on purpose: it pins what every\n"
    "# agent on this repo posts intents under — either a `repo` name, or a\n"
    "# `target_id` plus a readable `name`. No secrets belong here.\n"
)


def _pin_file(resolved: Resolved) -> str:
    """Render the pin file. An already-pinned id keeps its shape, including the
    `target` line and the readable name; everything else keeps the name-only
    shape. Nothing here ever invents an id."""
    if resolved.target_id:
        lines = []
        if resolved.target:
            lines.append(f'target = "{resolved.target}"')
        lines.append(f'target_id = "{resolved.target_id}"')
        lines.append(f'name = "{resolved.name}"')
    else:
        lines = [f'repo = "{resolved.name}"']
    lines.append(f'url = "{resolved.url}"')
    return _PIN_HEADER + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Managed-block placement
# --------------------------------------------------------------------------- #

def _place_block(original: str | None, block: str) -> str:
    """Return `original` with `block` inserted/updated. Three cases, in order:
    replace between existing markers; else replace a hand-pasted
    `## Intent registry` heading (up to the next `## ` or EOF); else append."""
    if original is None or original.strip() == "":
        return block

    lines = original.splitlines(keepends=True)

    begin = next(
        (i for i, ln in enumerate(lines)
         if ln.startswith(("<!-- flightplan:begin", _LEGACY_BEGIN_PREFIX))),
        None,
    )
    if begin is not None:
        end = next(
            (i for i, ln in enumerate(lines)
             if ln.startswith(("<!-- flightplan:end", _LEGACY_END_PREFIX))),
            None,
        )
        if end is not None and end >= begin:
            return "".join(lines[:begin]) + block + "".join(lines[end + 1:])

    heading = next(
        (i for i, ln in enumerate(lines) if ln.startswith("## Intent registry")),
        None,
    )
    if heading is not None:
        after = next(
            (j for j in range(heading + 1, len(lines)) if lines[j].startswith("## ")),
            len(lines),
        )
        rest = "".join(lines[after:])
        return "".join(lines[:heading]) + block + ("\n" + rest if rest else "")

    prefix = original if original.endswith("\n") else original + "\n"
    return prefix + "\n" + block


# --------------------------------------------------------------------------- #
# settings.json merge
# --------------------------------------------------------------------------- #

def _merge_settings(existing: dict) -> dict:
    """Ensure `hooks.Stop` contains our command, drop the legacy
    scripts/stop_hook.py wiring, and leave everything else untouched. Idempotent:
    re-running adds no duplicate."""
    settings = dict(existing)
    hooks = dict(settings.get("hooks") or {})
    stop = list(hooks.get("Stop") or [])

    new_stop: list = []
    have_ours = False
    for group in stop:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            new_stop.append(group)  # foreign shape — preserve verbatim
            continue
        kept = []
        for h in group["hooks"]:
            cmd = h.get("command") if isinstance(h, dict) else None
            if isinstance(cmd, str) and cmd.rstrip().rstrip('"').endswith(LEGACY_HOOK_SUFFIXES):
                continue  # legacy wiring — remove
            if cmd == STOP_HOOK_COMMAND:
                if have_ours:
                    continue  # collapse duplicates from an earlier buggy run
                have_ours = True
            kept.append(h)
        if kept:
            new_group = dict(group)
            new_group["hooks"] = kept
            new_stop.append(new_group)
        # a group whose hooks all dropped out disappears with it

    if not have_ours:
        new_stop.append(
            {"hooks": [{"type": "command", "command": STOP_HOOK_COMMAND}]}
        )

    hooks["Stop"] = new_stop
    settings["hooks"] = hooks
    return settings


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #

def run(
    root: Path,
    *,
    agent: str,
    repo: str | None,
    url: str | None,
    dry_run: bool,
    warnings: list[str] | None = None,
) -> dict[str, str]:
    """Install/update every per-repo artifact. Returns a map of root-relative
    path → `"written" | "updated" | "unchanged"`. Non-fatal issues (e.g. an
    unparseable existing settings.json) are appended to `warnings`."""
    warns = warnings if warnings is not None else []
    statuses: dict[str, str] = {}

    def write(rel: str, content: str) -> None:
        path = root / rel
        if path.exists():
            statuses[rel] = "unchanged" if path.read_text() == content else "updated"
        else:
            statuses[rel] = "written"
        if not dry_run and statuses[rel] != "unchanged":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    resolved = _resolve(root, repo, url)
    name = resolved.name

    # 1. The pin file — fully managed, regenerated each run, but a pinned id
    #    survives the regeneration untouched.
    write(config.PIN_FILENAME, _pin_file(resolved))
    # Migration (ROADMAP 35): the pre-rename pin is superseded by the file just
    # written — remove it so the repo doesn't carry two pin files.
    legacy_pin = root / LEGACY_PIN_REL
    if legacy_pin.exists():
        statuses[LEGACY_PIN_REL] = "removed"
        if not dry_run:
            legacy_pin.unlink()

    # 2. The agent snippet block, into CLAUDE.md and/or AGENTS.md.
    block = _snippet_block(name)
    snippet_targets = []
    if agent in ("claude", "both"):
        snippet_targets.append("CLAUDE.md")
    if agent in ("codex", "both"):
        snippet_targets.append("AGENTS.md")
    for fname in snippet_targets:
        path = root / fname
        original = path.read_text() if path.exists() else None
        write(fname, _place_block(original, block))

    # 3. Claude Code artifacts.
    if agent in ("claude", "both"):
        write(".claude/commands/registry-digest.md", _asset_text("registry-digest.md"))
        write(STOP_HOOK_REL, _asset_text("stop_hook.py"))
        # Migration (ROADMAP 35): the pre-rename vendored hook is superseded;
        # its settings wiring is dropped in _merge_settings below.
        legacy_hook = root / LEGACY_HOOK_REL
        if legacy_hook.exists():
            statuses[LEGACY_HOOK_REL] = "removed"
            if not dry_run:
                legacy_hook.unlink()

        settings_rel = ".claude/settings.json"
        settings_path = root / settings_rel
        existing_settings: dict | None
        if settings_path.exists():
            try:
                loaded = json.loads(settings_path.read_text())
                existing_settings = loaded if isinstance(loaded, dict) else None
            except (json.JSONDecodeError, ValueError):
                existing_settings = None
            if existing_settings is None:
                warns.append(
                    f"{settings_rel}: existing file is not a JSON object — left "
                    "unchanged; add the Stop hook by hand or fix the file and re-run."
                )
        else:
            existing_settings = {}
        if existing_settings is not None:
            merged = _merge_settings(existing_settings)
            write(settings_rel, json.dumps(merged, indent=2) + "\n")

    return statuses


# --------------------------------------------------------------------------- #
# Verify (advisory, printed, never fails the run)
# --------------------------------------------------------------------------- #

def _reachable(url: str) -> tuple[bool, str]:
    endpoint = url.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(endpoint, timeout=3) as resp:
            return 200 <= resp.status < 400, endpoint
    except Exception:
        return False, endpoint


# Registration classification. A name-only check accepts a registration that
# points at a dead git URL or someone's old local checkout forever, so we look
# at what the entry actually runs — the whole argv, and the url it talks to.
CURRENT = "current"   # runs exactly what we would register, against our url
STALE = "stale"       # registered, but points somewhere else
MISSING = "missing"   # no entry at all

LEGACY_SERVER_NAME = "intent-registry"


class Registration(NamedTuple):
    """What we found for the MCP server, and where."""
    status: str          # CURRENT | STALE | MISSING
    name: str | None     # the key it was registered under
    detail: str          # the command line it runs, for reporting


# NAME=VALUE argv tokens whose value must never be echoed. A legacy entry like
# `env FLIGHTPLAN_API_KEY=... uvx ...` carries the secret in args, not env.
_SECRET_TOKEN = re.compile(r"^([^=]*(?:KEY|TOKEN|SECRET|PASSWORD)[^=]*)=.+$", re.I)


def _redact(tokens: list[str]) -> list[str]:
    return [_SECRET_TOKEN.sub(r"\1=[redacted]", t) for t in tokens]


def _classify(entry: object, name: str, source: str, url: str) -> Registration:
    """CURRENT only if the entry runs exactly the argv we would register, under
    the branded name, with FLIGHTPLAN_URL set to `url`.

    The whole argv is compared against `_uvx_argv(source)` — the single source of
    truth — so a missing or wrong subcommand (`uvx getflightplan`,
    `uvx getflightplan install`) is caught too. A hand-written equivalent like
    `uvx --from getflightplan getflightplan mcp` reads STALE; that's fine, STALE
    is an advisory nudge plus a re-register offer, not an error.
    """
    if not isinstance(entry, dict):
        return Registration(STALE, name, "unrecognized entry")
    command = str(entry.get("command") or "")
    raw_args = entry.get("args") or []
    args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
    detail = " ".join(_redact([command, *args])).strip() or "(no command)"

    argv_ok = (
        name == "flightplan"
        and [Path(command).stem, *args] == _uvx_argv(source)
    )
    if not argv_ok:
        return Registration(STALE, name, detail)

    # Argv is right — now the url. Never report the API key, only the url.
    env = entry.get("env")
    found_url = env.get("FLIGHTPLAN_URL") if isinstance(env, dict) else None
    if found_url != url:
        where = f"registered URL {found_url}" if found_url else "no FLIGHTPLAN_URL set"
        return Registration(STALE, name, f"{detail} ({where}, expected {url})")
    return Registration(CURRENT, name, detail)


def _claude_registration(root: Path, source: str, url: str) -> Registration:
    """Find the MCP server entry in root .mcp.json, or in ~/.claude.json under
    the top-level mcpServers or any per-project mcpServers, and classify it.
    The branded name wins; the legacy `intent-registry` name is looked up second
    so a pre-rename install reads as stale rather than missing."""
    blocks: list[dict] = []
    for path in (root / ".mcp.json", Path.home() / ".claude.json"):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        blocks.append(data.get("mcpServers") or {})
        for proj in (data.get("projects") or {}).values():
            if isinstance(proj, dict):
                blocks.append(proj.get("mcpServers") or {})

    for name in ("flightplan", LEGACY_SERVER_NAME):
        for servers in blocks:
            if isinstance(servers, dict) and name in servers:
                return _classify(servers[name], name, source, url)
    return Registration(MISSING, None, "")


def _codex_registration(source: str, url: str) -> Registration:
    """Same for ~/.codex/config.toml, parsed as TOML. If the file won't parse we
    can't read the source, so a server table under either name counts as stale
    rather than crashing the run."""
    cfg = Path.home() / ".codex" / "config.toml"
    try:
        text = cfg.read_text() if cfg.exists() else ""
    except OSError:
        return Registration(MISSING, None, "")
    if not text.strip():
        return Registration(MISSING, None, "")

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        for name in ("flightplan", LEGACY_SERVER_NAME):
            # The `]` is optional: a missing bracket is one way the file broke.
            header = rf'^\s*\[\s*mcp_servers\s*\.\s*"?{re.escape(name)}"?\s*\]?\s*$'
            if re.search(header, text, re.M):
                return Registration(STALE, name, "unparseable ~/.codex/config.toml")
        return Registration(MISSING, None, "")

    servers = data.get("mcp_servers") or {}
    if isinstance(servers, dict):
        for name in ("flightplan", LEGACY_SERVER_NAME):
            if name in servers:
                return _classify(servers[name], name, source, url)
    return Registration(MISSING, None, "")


def _stop_key_available(root: Path) -> bool:
    if os.environ.get("FLIGHTPLAN_API_KEY", "").strip():
        return True
    if (Path.home() / ".config" / "flightplan" / "env").exists():
        return True
    return False


def verify(
    root: Path, *, agent: str, url: str, source: str = PACKAGE_SOURCE
) -> list[str]:
    """Advisory checks, one formatted line (or block) each. Never mutates
    anything; never prints a secret value. `source` is what a registration must
    run to count as current — the default, or whatever `--source` asked for."""
    lines: list[str] = []

    ok, endpoint = _reachable(url)
    lines.append(
        f"  ok   registry reachable at {endpoint}"
        if ok else
        f"  !!   registry unreachable at {endpoint} — check your network, or "
        "your --url / FLIGHTPLAN_URL override if you set one"
    )

    if agent in ("claude", "both"):
        reg = _claude_registration(root, source, url)
        if reg.status == CURRENT:
            lines.append("  ok   claude: flightplan MCP server is registered")
        elif reg.name == LEGACY_SERVER_NAME:
            # The legacy name still works, so this is a nudge, not a failure.
            lines.append(
                "  ok?  claude: registered under legacy name 'intent-registry' — "
                "re-register:\n"
                "         claude mcp remove intent-registry && claude mcp add "
                f"flightplan -- {_uvx_command(source)}"
            )
        elif reg.status == STALE:
            lines.append(
                f"  ok?  claude: flightplan is registered but runs {reg.detail} "
                f"— not the current {_uvx_command(source)}. Re-register:\n"
                "         claude mcp remove flightplan --scope user && "
                "claude mcp add flightplan --scope user "
                "--env FLIGHTPLAN_URL=... --env FLIGHTPLAN_API_KEY=... "
                f"-- {_uvx_command(source)}"
            )
        else:
            lines.append(
                "  !!   claude: flightplan MCP server not found in .mcp.json "
                "or ~/.claude.json — register it:\n"
                "         claude mcp add flightplan --scope user "
                "--env FLIGHTPLAN_URL=... --env FLIGHTPLAN_API_KEY=... "
                f"-- {_uvx_command(source)}\n"
                "       (set FLIGHTPLAN_URL and FLIGHTPLAN_API_KEY in the server "
                "env; values not shown here)"
            )
        if _stop_key_available(root):
            lines.append("  ok   stop hook: an API key source is available")
        else:
            lines.append(
                "  !!   stop hook: no API key found (FLIGHTPLAN_API_KEY env "
                "or ~/.config/flightplan/env) — the hook "
                "will silently allow stops until one exists; put your key in "
                "~/.config/flightplan/env as FLIGHTPLAN_API_KEY=..."
            )

    if agent in ("codex", "both"):
        reg = _codex_registration(source, url)
        args = ", ".join(f'"{a}"' for a in _uvx_argv(source)[1:])
        block = (
            "         [mcp_servers.flightplan]\n"
            '         command = "uvx"\n'
            f"         args = [{args}]\n"
            f'         env = {{ FLIGHTPLAN_URL = "{url}", '
            'FLIGHTPLAN_API_KEY = "<your-key>" }'
        )
        if reg.status == CURRENT:
            lines.append("  ok   codex: flightplan MCP server is registered")
        elif reg.status == STALE:
            where = (
                f"under legacy name '{reg.name}'" if reg.name == LEGACY_SERVER_NAME
                else f"runs {reg.detail} — not the current {_uvx_command(source)}"
            )
            lines.append(
                f"  ok?  codex: flightplan is registered but {where}. "
                f"Replace it in ~/.codex/config.toml with:\n{block}"
            )
        else:
            lines.append(
                "  !!   codex: flightplan not found in ~/.codex/config.toml — "
                f"add:\n{block}"
            )

    return lines


# --------------------------------------------------------------------------- #
# Interactive onboarding (decision 2bdbf56c)
# --------------------------------------------------------------------------- #
#
# The one-command onboarding: prompt once for the API key and fan it out to
# the places that need it (the stop-hook env file and the agent MCP
# registrations, claude and codex). This layer lives outside `run()` — run() stays pure and
# testable — and is only reached from `main()` when attached to a real TTY with
# `--no-input` absent. Secrets are never echoed: not the key, not any command
# line carrying it, not subprocess output.

# The manual-registration lines printed when we can't (or won't) auto-register.

def _claude_guidance(source: str = PACKAGE_SOURCE) -> str:
    return (
        "  →    register the flightplan MCP server by hand:\n"
        "         claude mcp add flightplan --scope user "
        "--env FLIGHTPLAN_URL=... --env FLIGHTPLAN_API_KEY=... "
        f"-- {_uvx_command(source)}"
    )


def _codex_guidance(source: str = PACKAGE_SOURCE) -> str:
    return (
        "  →    register the flightplan MCP server by hand:\n"
        "         codex mcp add flightplan "
        "--env FLIGHTPLAN_URL=... --env FLIGHTPLAN_API_KEY=... "
        f"-- {_uvx_command(source)}"
    )


def _key_config_file() -> Path:
    """The stop hook's last-resort key source, ~/.config/flightplan/env.
    Resolved at call time so HOME (and tests) take effect."""
    return Path.home() / ".config" / "flightplan" / "env"


def _env_file_lines(path: Path) -> list[str]:
    """The lines of the env file, or an empty list. An unreadable file counts
    as an empty one — the caller writes a new file over it."""
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


def _write_env_lines(path: Path, lines: list[str]) -> None:
    """Replace the env file with `lines`, atomically and with mode 600.

    The secret is never in a world-readable file, not even for an instant: the
    temporary file gets mode 600 before any content goes in, and `os.replace`
    then moves it over the target in one step. A reader sees the old file or
    the new one, never a half-written one.
    """
    temporary = path.with_name(path.name + ".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write("".join(f"{line}\n" for line in lines))
        os.chmod(temporary, 0o600)  # O_CREAT skips the mode on a leftover file
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _write_key_file(key: str) -> Path:
    """Persist the key to ~/.config/flightplan/env (mode 600, dir 700) so
    the stop hook can find it. Any other line in the file stays. Returns the
    path; never echoes the key."""
    env_file = _key_config_file()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(env_file.parent, 0o700)  # explicit: mkdir won't chmod a pre-existing dir
    others = [
        line for line in _env_file_lines(env_file)
        if not line.startswith("FLIGHTPLAN_API_KEY=")
    ]
    _write_env_lines(env_file, [*others, f"FLIGHTPLAN_API_KEY={key}"])
    return env_file


def _remove_key_line() -> bool:
    """Drop the FLIGHTPLAN_API_KEY line from ~/.config/flightplan/env. Every
    other line stays, and the file keeps mode 600. Returns True if a key was
    there to remove."""
    env_file = _key_config_file()
    lines = _env_file_lines(env_file)
    kept = [line for line in lines if not line.startswith("FLIGHTPLAN_API_KEY=")]
    if len(kept) == len(lines):
        return False
    _write_env_lines(env_file, kept)
    return True


def _read_key_file() -> str:
    """The key a previous install saved to ~/.config/flightplan/env, or "".
    Without this, a Codex-first install would save the key and a later Claude
    install would see "a key exists" yet be unable to register with it."""
    path = _key_config_file()
    try:
        if path.exists():
            for line in path.read_text().splitlines():
                if line.startswith("FLIGHTPLAN_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _run_claude_register(url: str, key: str, source: str) -> bool:
    """`claude mcp add` for the flightplan server, no shell. Returns success by
    exit code only. The argv carries the key, so we never print the command and
    suppress the child's stdout/stderr rather than risk echoing it back."""
    cmd = [
        "claude", "mcp", "add", "flightplan", "--scope", "user",
        "--env", f"FLIGHTPLAN_URL={url}",
        "--env", f"FLIGHTPLAN_API_KEY={key}",
        "--", *_uvx_argv(source),
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _run_codex_register(url: str, key: str, source: str) -> bool:
    """`codex mcp add` for the flightplan server — same secrecy rules as
    `_run_claude_register`: the argv carries the key, so never print the
    command and suppress the child's output."""
    cmd = [
        "codex", "mcp", "add", "flightplan",
        "--env", f"FLIGHTPLAN_URL={url}",
        "--env", f"FLIGHTPLAN_API_KEY={key}",
        "--", *_uvx_argv(source),
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _mcp_remove(binary: str, name: str) -> None:
    """Drop an existing registration before re-adding it: `mcp add` on a name
    that already exists is an error in some CLI versions, and a legacy-named
    entry has to go anyway. Failure is fine — the add is what matters."""
    cmd = [binary, "mcp", "remove", name]
    if binary == "claude":
        cmd += ["--scope", "user"]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _offer_to_fix(root: Path, *, agent: str, url: str, source: str) -> None:
    """Interactive fixes for whatever verify found missing. The key is asked
    for at most once (env → the saved key file → prompt); registration is
    offered per agent with a default of yes — the user invoked an installer
    whose stated job includes registration — and after any mutation the checks
    are re-run so the user sees the verified end state. Caller guards the
    TTY/--no-input/--dry-run preconditions."""
    # One key, three sources in order; the prompt fires at most once.
    key = os.environ.get("FLIGHTPLAN_API_KEY", "").strip() or _read_key_file()
    if not key:
        entered = getpass.getpass(
            "FlightPlan API key (from your team admin; blank to skip): "
        ).strip()
        if entered:
            _write_key_file(entered)
            print("  written  ~/.config/flightplan/env")
            key = entered

    mutated = False

    def offer(name: str, reg: Registration, register, guidance: str) -> None:
        """One registration offer: run it on yes (the default), fall back to
        the manual guidance on decline, failure, or a missing binary. A stale
        registration gets the same offer, worded as a replacement."""
        nonlocal mutated
        if reg.status == CURRENT:
            return
        stale = reg.status == STALE
        if stale:
            print(f"  ok?  {name}: existing registration runs {reg.detail}")
        verb = "Re-register" if stale else "Register"
        if shutil.which(name) and key:
            answer = input(
                f"{verb} the flightplan MCP server with {name} now? [Y/n] "
            ).strip().lower()
            if answer in ("", "y", "yes"):
                if stale and reg.name:
                    _mcp_remove(name, reg.name)
                if register(url, key, source):
                    print(f"  registered  flightplan MCP server ({name})")
                    mutated = True
                else:
                    print(f"  !!   {name} mcp add failed — do it by hand:")
                    print(guidance)
            else:
                print(guidance)
        elif not shutil.which(name):
            print(guidance)
        # else: binary present but still no key — can't register; verify's
        # guidance line already covered the manual path.

    if agent in ("claude", "both"):
        offer(
            "claude",
            _claude_registration(root, source, url),
            _run_claude_register,
            _claude_guidance(source),
        )
    if agent in ("codex", "both"):
        offer("codex", _codex_registration(source, url), _run_codex_register,
              _codex_guidance(source))

    if mutated:
        print("re-verify:")
        for line in verify(root, agent=agent, url=url, source=source):
            print(line)
        print("  →    start a new agent session in this repo to pick up the "
              "registration.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        top = out.stdout.strip()
        if out.returncode == 0 and top:
            return Path(top)
    except Exception:
        pass
    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="getflightplan install",
        description="Install/update this repo's FlightPlan artifacts, idempotently.",
    )
    parser.add_argument(
        "--agent", choices=["claude", "codex", "both"], default="claude",
        help="which agent's artifacts to write (default: claude)",
    )
    parser.add_argument("--repo", default=None, help="set or change the pinned repo name")
    parser.add_argument("--url", default=None, help="override the registry url to pin (testing; defaults to the hosted service)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="compute and report statuses, write nothing",
    )
    parser.add_argument(
        "--no-input", action="store_true",
        help="never prompt (for CI / non-interactive use)",
    )
    parser.add_argument(
        "--source", default=PACKAGE_SOURCE,
        help="what the registered MCP server runs (default: the PyPI package "
        "`getflightplan`; pass a git URL or a local path for development, and "
        "it is registered via `uvx --from`)",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    warnings: list[str] = []
    statuses = run(
        root,
        agent=args.agent,
        repo=args.repo,
        url=args.url,
        dry_run=args.dry_run,
        warnings=warnings,
    )

    print(f"getflightplan install{' (dry run)' if args.dry_run else ''} — {root}")
    for rel in sorted(statuses):
        print(f"  {statuses[rel]:<9} {rel}")
    for w in warnings:
        print(f"  !!   {w}")

    resolved_url = _resolve(root, args.repo, args.url).url
    print("verify:")
    for line in verify(root, agent=args.agent, url=resolved_url, source=args.source):
        print(line)

    # Interactive one-command onboarding — offer to fill what verify flagged.
    # Only when we're actually attached to a terminal and allowed to prompt.
    if sys.stdin.isatty() and not args.no_input and not args.dry_run:
        _offer_to_fix(root, agent=args.agent, url=resolved_url, source=args.source)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
