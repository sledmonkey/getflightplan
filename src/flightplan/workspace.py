"""Project pins: one folder that binds several repositories.

A `.flightplan.toml` with `target = "project"` marks a workspace — an umbrella
directory that is usually not a git repo itself, whose immediate child
directories are separate repos, each with its own committed repository pin:

    workspace/                 .flightplan.toml  target = "project"
      service/                 .flightplan.toml  target = "repository"
      client/                  .flightplan.toml  target = "repository"
      CLAUDE.md

An agent working from the workspace root talks about `service/src/api.py`; an
agent working inside `service/` talks about `src/api.py`. Both mean one file,
and collision checks compare strings, so the client has to reconcile them. It
does that in two steps:

  - the workspace root replaces the git root as the bound root for path
    canonicalization (see paths.py) — the workspace has no git root of its own;
  - each canonical value is then mapped back to a child repository by its first
    path segment, which is what lets a workspace-scoped post carry per-repo
    paths that match what agents inside those repos send.

Values that do not start with a discovered child directory — pure patterns like
`**/*.py`, files at the workspace root, anything under a directory with no pin
— have no repository to belong to. They stay workspace-relative, and their
presence is what forces a post to stay workspace-scoped.

The nearest pin wins, so a session inside a child repo never reaches this
module: it finds that repo's own pin and behaves exactly as it always has.

Stdlib only.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import NamedTuple

from . import config

# Characters that make a path segment a pattern.
_WILDCARDS = "*?["


class Child(NamedTuple):
    """A child repository of the workspace, as its own pin describes it."""

    dir: str                # the directory name, i.e. the leading path segment
    target_id: str
    name: str | None


class Workspace(NamedTuple):
    """A project pin and the child repositories under it."""

    root: Path
    pin: config.Pin
    children: dict[str, Child]      # directory name → child

    def map_value(self, value: str) -> tuple[str | None, str]:
        """Map one workspace-relative value to (child dir, repo-local value).

        The child dir is None when the value belongs to no repository — then
        the value comes back unchanged, still workspace-relative.
        """
        segment, _, rest = value.partition("/")
        if segment not in self.children:
            return None, value
        # The whole repo, written either way.
        if not rest or rest == "**":
            return segment, "**"
        return segment, rest

    def group(self, values: list[str] | None) -> tuple[dict[str, list[str]], list[str]]:
        """Split values into {child dir: repo-local values} plus the ones that
        map to no repository. Order is preserved so the wire is deterministic."""
        mapped: dict[str, list[str]] = {}
        unmapped: list[str] = []
        for value in values or []:
            child_dir, local = self.map_value(value)
            if child_dir is None:
                unmapped.append(local)
            else:
                mapped.setdefault(child_dir, []).append(local)
        return mapped, unmapped

    def split(self, values: list[str] | None, field: str) -> list[dict]:
        """The `repositories` body field: one entry per child repository the
        values reach, carrying that repo's own paths under `field`."""
        mapped, _ = self.group(values)
        return [
            {"target_id": self.children[d].target_id, field: local}
            for d, local in mapped.items()
        ]

    def fan_out(self, values: list[str] | None) -> dict[str, list[str]]:
        """The globs for each child repository in a collision check.

        This mapping is wider than `group`. A first segment of `**` can match
        a path at any depth, so every child gets the full pattern. A first
        segment with a wildcard, like `agent*`, is matched against the child
        directory names. Each match gets the remainder, or `**` when there is
        no remainder. A concrete first segment maps as `map_value` does.

        Only reads use this mapping. A read that asks too widely is safe. A
        post must not project work into a repository it may not touch, so
        posts keep the strict mapping in `group`.
        """
        mapped: dict[str, list[str]] = {}

        def add(child_dir: str, glob: str) -> None:
            mapped.setdefault(child_dir, []).append(glob)

        for value in values or []:
            segment, _, rest = value.partition("/")
            if segment == "**":
                for d in self.children:
                    add(d, value)
            elif any(ch in segment for ch in _WILDCARDS):
                for d in self.children:
                    if fnmatch(d, segment):
                        add(d, rest or "**")
            else:
                child_dir, local = self.map_value(value)
                if child_dir is not None:
                    add(child_dir, local)
        return mapped

    def child_by_id(self, target_id: str) -> Child | None:
        """The child repository with this id, or None if none is pinned here."""
        for child in self.children.values():
            if child.target_id == target_id:
                return child
        return None


def discover(root: Path) -> dict[str, Child]:
    """Child repositories: immediate subdirectories whose own pin names a
    repository id. A legacy name-only pin has no id, so it cannot be mapped —
    it is skipped, and paths under it stay workspace-relative."""
    children: dict[str, Child] = {}
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return children
    for d in entries:
        pin_file = d / config.PIN_FILENAME
        try:
            if not pin_file.is_file():
                continue
            pin = config.read_pin(pin_file.read_text())
        except OSError:
            continue
        if pin.target == "repository" and pin.target_id:
            children[d.name] = Child(d.name, pin.target_id, pin.name)
    return children


def bind(start: Path | None = None) -> tuple[config.Pin, Workspace | None]:
    """The nearest pin, plus the workspace it binds when it is a project pin.

    Read fresh on every call — the pin file and the set of child repos are
    both things a session can change under us. Never raises: an unreadable pin
    is the same as no pin.
    """
    path = config.find_pin_file(start)
    if path is None:
        return config.EMPTY, None
    try:
        pin = config.read_pin(path.read_text())
    except OSError:
        return config.EMPTY, None
    if pin.target != "project":
        return pin, None
    root = path.parent.resolve()
    return pin, Workspace(root, pin, discover(root))
