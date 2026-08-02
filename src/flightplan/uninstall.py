"""Client uninstall (`getflightplan uninstall`, ROADMAP 37).

Reverses what `getflightplan install` wrote in this repo:

  - the managed snippet block in `CLAUDE.md` and `AGENTS.md` (the file itself
    is deleted only if the block was its entire content),
  - `.flightplan.toml` (and the legacy pin, if present),
  - the `/registry-digest` command and the stop hook,
  - the stop hook's `Stop` wiring in `.claude/settings.json` (everything else
    in that file is preserved).

MCP registrations (`claude mcp` / `codex mcp`) are machine-level, not
per-repo — removing one affects every repo on this machine — so they are only
removed on explicit confirmation. The saved API key
(`~/.config/flightplan/env`) is machine-level too and is kept unless
`--purge-key` is passed.

Advisory like install: missing pieces are reported, never an error. Stdlib
only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .install import (
    LEGACY_HOOK_REL,
    LEGACY_HOOK_SUFFIXES,
    LEGACY_PIN_REL,
    STOP_HOOK_COMMAND,
    STOP_HOOK_REL,
    _LEGACY_BEGIN_PREFIX,
    _LEGACY_END_PREFIX,
    _key_config_file,
    _repo_root,
)

DIGEST_REL = ".claude/commands/registry-digest.md"
SETTINGS_REL = ".claude/settings.json"
SNIPPET_FILES = ("CLAUDE.md", "AGENTS.md")


# --------------------------------------------------------------------------- #
# Managed-block removal
# --------------------------------------------------------------------------- #

def _strip_block(original: str) -> str | None:
    """Return `original` without the managed block (current or legacy
    markers), or None if no block is present. A blank line left behind where
    the block sat is collapsed."""
    lines = original.splitlines(keepends=True)
    begin = next(
        (i for i, ln in enumerate(lines)
         if ln.startswith(("<!-- flightplan:begin", _LEGACY_BEGIN_PREFIX))),
        None,
    )
    if begin is None:
        return None
    end = next(
        (i for i, ln in enumerate(lines)
         if ln.startswith(("<!-- flightplan:end", _LEGACY_END_PREFIX))),
        None,
    )
    if end is None or end < begin:
        return None  # orphaned marker — don't guess at the block's extent
    before, after = lines[:begin], lines[end + 1:]
    # Collapse the seam: one blank line at most between the halves.
    while before and before[-1].strip() == "":
        before.pop()
    while after and after[0].strip() == "":
        after.pop(0)
    if before and after:
        return "".join(before) + "\n\n" + "".join(after)
    return "".join(before) + "".join(after)


# --------------------------------------------------------------------------- #
# settings.json un-merge
# --------------------------------------------------------------------------- #

# Every command suffix that identifies our Stop wiring, past or present: the
# current hook, plus the legacy variants install.py recognizes — a legacy
# repo may be uninstalled directly, without an upgrade install in between.
_OUR_HOOK_SUFFIXES = (STOP_HOOK_REL, *LEGACY_HOOK_SUFFIXES)


def _unmerge_settings(existing: dict) -> dict:
    """Remove our Stop command (current or legacy) from `hooks.Stop`,
    dropping any group/key left empty. Everything else is preserved
    verbatim — the mirror of install's `_merge_settings`."""
    settings = dict(existing)
    hooks = dict(settings.get("hooks") or {})
    stop = list(hooks.get("Stop") or [])

    new_stop: list = []
    for group in stop:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            new_stop.append(group)  # foreign shape — preserve verbatim
            continue
        kept = [
            h for h in group["hooks"]
            if not (
                isinstance(h, dict)
                and isinstance(h.get("command"), str)
                and (
                    h["command"] == STOP_HOOK_COMMAND
                    or h["command"].rstrip().rstrip('"').endswith(_OUR_HOOK_SUFFIXES)
                )
            )
        ]
        if kept:
            new_group = dict(group)
            new_group["hooks"] = kept
            new_stop.append(new_group)

    if new_stop:
        hooks["Stop"] = new_stop
    else:
        hooks.pop("Stop", None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #

def run(
    root: Path,
    *,
    dry_run: bool,
    purge_key: bool = False,
    warnings: list[str] | None = None,
) -> dict[str, str]:
    """Remove every per-repo artifact install wrote. Returns a map of
    root-relative path → `"removed" | "updated" | "absent"` ("updated" means
    our part was removed and the rest of the file kept)."""
    warns = warnings if warnings is not None else []
    statuses: dict[str, str] = {}

    def remove(rel: str) -> None:
        path = root / rel
        if path.exists():
            statuses[rel] = "removed"
            if not dry_run:
                path.unlink()
        else:
            statuses[rel] = "absent"

    # 1. Snippet blocks. Delete the file only when the block was all of it.
    for fname in SNIPPET_FILES:
        path = root / fname
        if not path.exists():
            statuses[fname] = "absent"
            continue
        stripped = _strip_block(path.read_text())
        if stripped is None:
            statuses[fname] = "absent"  # file exists but holds no managed block
        elif stripped.strip() == "":
            statuses[fname] = "removed"
            if not dry_run:
                path.unlink()
        else:
            statuses[fname] = "updated"
            if not dry_run:
                path.write_text(stripped)

    # 2. Fully-managed files.
    remove(".flightplan.toml")
    remove(LEGACY_PIN_REL)
    remove(DIGEST_REL)
    remove(STOP_HOOK_REL)
    remove(LEGACY_HOOK_REL)

    # 3. Stop wiring in settings.json.
    settings_path = root / SETTINGS_REL
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, ValueError):
            loaded = None
        if not isinstance(loaded, dict):
            statuses[SETTINGS_REL] = "absent"
            warns.append(
                f"{SETTINGS_REL}: existing file is not a JSON object — left "
                "unchanged; remove the Stop hook entry by hand."
            )
        else:
            unmerged = _unmerge_settings(loaded)
            if unmerged == loaded:
                statuses[SETTINGS_REL] = "absent"  # no wiring of ours present
            elif unmerged == {}:
                statuses[SETTINGS_REL] = "removed"
                if not dry_run:
                    settings_path.unlink()
            else:
                statuses[SETTINGS_REL] = "updated"
                if not dry_run:
                    settings_path.write_text(json.dumps(unmerged, indent=2) + "\n")
    else:
        statuses[SETTINGS_REL] = "absent"

    # 4. Directories left empty by the removals.
    if not dry_run:
        for rel in (".claude/hooks", ".claude/commands", ".claude"):
            d = root / rel
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

    # 5. The machine-level key, only on request.
    if purge_key:
        key_file = _key_config_file()
        if key_file.exists():
            statuses["~/.config/flightplan/env"] = "removed"
            if not dry_run:
                key_file.unlink()
                try:
                    if not any(key_file.parent.iterdir()):
                        key_file.parent.rmdir()
                except OSError:
                    pass
        else:
            statuses["~/.config/flightplan/env"] = "absent"

    return statuses


# --------------------------------------------------------------------------- #
# MCP deregistration (interactive, machine-level)
# --------------------------------------------------------------------------- #

def _offer_mcp_removal() -> None:
    """Offer to remove the `flightplan` MCP registration per agent CLI.
    Default is no: the registration is machine-level, so removing it breaks
    FlightPlan in every other repo on this machine too."""
    for name in ("claude", "codex"):
        if not shutil.which(name):
            continue
        answer = input(
            f"Remove the flightplan MCP registration from {name}? This "
            "affects every repo on this machine. [y/N] "
        ).strip().lower()
        if answer in ("y", "yes"):
            try:
                proc = subprocess.run(
                    [name, "mcp", "remove", "flightplan"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                ok = proc.returncode == 0
            except Exception:
                ok = False
            print(
                f"  removed  flightplan MCP registration ({name})" if ok
                else f"  !!   {name} mcp remove failed — run "
                f"`{name} mcp remove flightplan` by hand"
            )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="getflightplan uninstall",
        description="Remove this repo's FlightPlan artifacts.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be removed, remove nothing",
    )
    parser.add_argument(
        "--purge-key", action="store_true",
        help="also delete the saved API key (~/.config/flightplan/env); "
        "kept by default because other repos on this machine may use it",
    )
    parser.add_argument(
        "--no-input", action="store_true",
        help="never prompt (skips the MCP deregistration offer)",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    warnings: list[str] = []
    statuses = run(
        root, dry_run=args.dry_run, purge_key=args.purge_key, warnings=warnings,
    )

    print(f"getflightplan uninstall{' (dry run)' if args.dry_run else ''} — {root}")
    for rel in sorted(statuses):
        print(f"  {statuses[rel]:<9} {rel}")
    for w in warnings:
        print(f"  !!   {w}")
    if not args.purge_key and _key_config_file().exists():
        print(
            "  kept      ~/.config/flightplan/env (machine-level key; "
            "pass --purge-key to remove)"
        )

    if sys.stdin.isatty() and not args.no_input and not args.dry_run:
        _offer_mcp_removal()
    elif not args.dry_run:
        print(
            "  →    MCP registrations are machine-level and were left alone; "
            "remove with `claude mcp remove flightplan` / "
            "`codex mcp remove flightplan` if this was the last repo using "
            "FlightPlan."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
