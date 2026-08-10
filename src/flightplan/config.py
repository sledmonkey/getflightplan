"""The `.flightplan.toml` pin — one parser, three shapes.

The committed pin file is what makes every agent on a repo post under the same
thing. Three shapes are read:

    repo = "coolproject"                     # a readable name only
    url = "https://api.getflightplan.com"

    target = "repository"                    # a pinned id, plus a readable name
    target_id = "repo_9f3c2a"
    name = "coolproject"
    url = "https://api.getflightplan.com"

    target = "project"                       # a workspace of several repos
    target_id = "proj_5b71ee"
    name = "coolproject rewrite"
    url = "https://api.getflightplan.com"

The pinned shapes are written only when an id is already pinned; the installer
never invents one. The third sits on a workspace directory whose children are
the repos — same fields, and what changes is how the client reads paths under
it (workspace.py). All three stay supported, so this module is the single place
that knows how to read any of them.

Parsed with a tolerant line regex, not tomllib: the vendored stop hook runs
under whatever `python3` is on PATH and carries its own copy of this logic, so
the two parsers must behave the same.

Stdlib only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

PIN_FILENAME = ".flightplan.toml"


class Pin(NamedTuple):
    """What the pin file says. Every field is optional — an absent or
    unreadable file gives an all-`None` pin."""

    name: str | None        # readable name: new `name`, else legacy `repo`
    target_id: str | None   # opaque id, e.g. "repo_9f3c2a"; None in the legacy shape
    target: str | None      # what the id refers to, e.g. "repository"
    url: str | None


EMPTY = Pin(None, None, None, None)


def _value(text: str, key: str) -> str | None:
    """`key = "value"` out of the pin file. Line-anchored, so `target_id` never
    matches a request for `id`."""
    m = re.compile(rf'^\s*{re.escape(key)}\s*=\s*"([^"]+)"', re.M).search(text)
    return m.group(1) if m else None


def read_pin(text: str) -> Pin:
    """Parse pin-file text. `name` wins over `repo` when both are present."""
    return Pin(
        name=_value(text, "name") or _value(text, "repo"),
        target_id=_value(text, "target_id"),
        target=_value(text, "target"),
        url=_value(text, "url"),
    )


def find_pin_file(start: Path | None = None) -> Path | None:
    """The nearest `.flightplan.toml`, walking `start` (default cwd) upward."""
    here = start or Path.cwd()
    for d in (here, *here.parents):
        candidate = d / PIN_FILENAME
        if candidate.exists():
            return candidate
    return None


def find_pin(start: Path | None = None) -> Pin:
    """The nearest pin, or an empty one. Never raises — a missing or unreadable
    pin file just means nothing is pinned."""
    path = find_pin_file(start)
    if path is None:
        return EMPTY
    try:
        # UTF-8 explicitly: TOML is UTF-8 by specification, and a pinned
        # repository name can hold any text. Letting the locale decide makes
        # an accented name unreadable under `LC_ALL=C`.
        return read_pin(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return EMPTY
