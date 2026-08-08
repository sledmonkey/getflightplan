"""Canonical repo-relative paths for everything the client puts on the wire.

Agents call from wherever their shell happens to be: the repo root, a nested
subdirectory, a linked worktree. Collision checks compare strings, so the same
file has to arrive as the same string from all three. This module rewrites
`touches`, `files`, and `overlaps` values into repo-relative POSIX paths before
they are sent.

The rules, in order:

  - Absolute paths inside the repository become root-relative.
  - `./x` and `../x` are explicitly cwd-relative, so they are resolved against
    the cwd.
  - A bare relative value is rewritten when it actually exists relative to the
    cwd; otherwise it is left as written and read as already repo-relative.
    That reading is unambiguous rather than a guess: agents are instructed to
    send repo-relative paths, so a value with nothing under the cwd to match is
    already in the form the registry stores. (At the repo root the two readings
    are the same thing.)
  - Only the concrete leading portion of a glob is touched. `**/*.py` has none
    and passes through; `src/*` keeps its `*`.
  - Anything landing outside the repository — an absolute path elsewhere on the
    machine, or `../` traversal escaping the root — is REJECTED with a
    ValueError. There is no second namespace to store it in, so it must not be
    silently sent. A `..` segment anywhere past the first wildcard is rejected
    too: nothing resolves it there, so it would travel unchecked.

Outside a git repository there is no bound root, nothing is rewritten, and
nothing is rejected. Stdlib only — subprocess git and pathlib, no dependency
beyond the thin client's own.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_WILDCARD = re.compile(r"[*?\[]")


def repo_root(cwd: Path | None = None) -> Path | None:
    """The repository root, or None outside a repo. `--show-toplevel` reports
    the linked worktree's own root, which is exactly what we want: a file in a
    worktree canonicalizes the same as that file in the main checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    top = out.stdout.strip()
    if out.returncode != 0 or not top:
        return None
    return Path(top).resolve()


def _split_pattern(value: str) -> tuple[str, str]:
    """Split into (concrete leading path, glob tail) at the first segment that
    holds a wildcard: `src/api/*.py` → (`src/api`, `*.py`), `**/*.py` → (``,
    `**/*.py`)."""
    parts = value.split("/")
    for i, part in enumerate(parts):
        if _WILDCARD.search(part):
            return "/".join(parts[:i]), "/".join(parts[i:])
    return value, ""


class OutsideRepository(ValueError):
    """A value that cannot be mapped into the repository it was sent from."""


def _relativize(head: str, root: Path, cwd: Path) -> str | None:
    """`head` as a root-relative POSIX string, or None to leave it as written.
    Returns "" when head *is* the root. Raises OutsideRepository when it lands
    outside the root."""
    p = Path(head)
    leave_as_written = False
    if p.is_absolute():
        candidate = p
    elif head in (".", "..") or head.startswith(("./", "../")):
        candidate = cwd / p      # explicitly cwd-relative
    elif cwd == root:
        candidate = root / p     # at the root both readings agree
    elif (cwd / p).exists():
        candidate = cwd / p      # really there: the caller meant cwd-relative
    else:
        # Already repo-relative. Still checked against the root, so traversal
        # buried in the value can't slip through unnormalized.
        candidate = root / p
        leave_as_written = True
    try:
        rel = candidate.resolve().relative_to(root)
    except ValueError:
        raise OutsideRepository(head) from None
    if leave_as_written:
        return None
    return "" if rel == Path(".") else rel.as_posix()


def normalize(value: str, root: Path, cwd: Path) -> str:
    """One path or glob, canonicalized against `root`. Raises
    OutsideRepository if it points outside."""
    if not value:
        return value
    head, tail = _split_pattern(value)
    # Head resolution below settles any `..` in the concrete prefix. Past the
    # first wildcard nothing resolves it, so `**/../../x` would escape the
    # check entirely — refuse it instead.
    if ".." in tail.split("/"):
        raise OutsideRepository(value)
    if not head:
        return value             # pure pattern, e.g. `**/*.py`
    rel = _relativize(head, root, cwd)
    if rel is None:
        return value
    if not rel:
        return tail or "."
    return f"{rel}/{tail}" if tail else rel


def normalize_all(values: list[str] | None) -> list[str] | None:
    """Canonicalize a list of paths/globs.

    Raises OutsideRepository, naming every offending entry, if any of them
    resolve outside the repository. Outside a git repo there is no root to
    violate and the values pass through unchanged.
    """
    if not values:
        return values
    cwd = Path.cwd().resolve()
    root = repo_root(cwd)
    if root is None:
        return list(values)

    normalized: list[str] = []
    rejected: list[str] = []
    for v in values:
        try:
            normalized.append(normalize(v, root, cwd))
        except OutsideRepository:
            rejected.append(v)
    if rejected:
        raise OutsideRepository(
            "paths must be inside this repository and are recorded "
            f"repository-relative. These resolve outside {root}: "
            + ", ".join(rejected)
            + ". Rewrite them relative to the repository root."
        )
    return normalized
