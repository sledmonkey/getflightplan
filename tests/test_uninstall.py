"""Uninstall-kit tests (ROADMAP 37). Mirror of the install tests: pure
filesystem work over a tmp_path repo — install, then uninstall, and assert
the tree returns to where it started while user content survives."""

import json
import subprocess
from pathlib import Path

from flightplan import install, uninstall


def _git_init(path: Path, origin: str | None = None) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)


def _snapshot(root: Path) -> dict:
    return {
        p.relative_to(root): p.read_text()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }


def test_install_then_uninstall_restores_tree(tmp_path):
    _git_init(tmp_path, "https://github.com/acme/coolproject.git")
    before = _snapshot(tmp_path)

    install.run(tmp_path, agent="both", repo=None, url=None, dry_run=False)
    statuses = uninstall.run(tmp_path, dry_run=False)

    assert _snapshot(tmp_path) == before
    assert not (tmp_path / ".claude").exists()  # emptied dirs are removed too
    assert statuses["CLAUDE.md"] == "removed"
    assert statuses["AGENTS.md"] == "removed"
    assert statuses[".flightplan.toml"] == "removed"
    assert statuses[".claude/settings.json"] == "removed"


def test_user_content_survives(tmp_path):
    _git_init(tmp_path, "https://github.com/acme/coolproject.git")
    (tmp_path / "CLAUDE.md").write_text("# My repo\n\nHouse rules.\n")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "echo user-hook"},
        ]}]},
    }))

    install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)
    statuses = uninstall.run(tmp_path, dry_run=False)

    # The snippet block is gone; the user's own content is intact.
    assert statuses["CLAUDE.md"] == "updated"
    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert "House rules." in claude_md
    assert "flightplan" not in claude_md

    # Our Stop wiring is gone; the user's hook and permissions remain.
    assert statuses[".claude/settings.json"] == "updated"
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
    commands = [
        h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]
    ]
    assert commands == ["echo user-hook"]
    # .claude still exists — it holds the user's settings file.
    assert (tmp_path / ".claude").is_dir()


def test_uninstall_on_clean_repo_is_a_noop(tmp_path):
    _git_init(tmp_path)
    statuses = uninstall.run(tmp_path, dry_run=False)
    assert set(statuses.values()) == {"absent"}
    assert _snapshot(tmp_path) == {}


def test_dry_run_removes_nothing(tmp_path):
    _git_init(tmp_path, "https://github.com/acme/coolproject.git")
    install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)
    before = _snapshot(tmp_path)

    statuses = uninstall.run(tmp_path, dry_run=True)

    assert _snapshot(tmp_path) == before
    assert statuses[".flightplan.toml"] == "removed"  # reported, not performed


def test_purge_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    key_file = install._write_key_file("sekret")
    assert key_file.exists()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    # Default: the machine-level key survives an uninstall.
    statuses = uninstall.run(repo, dry_run=False)
    assert "~/.config/flightplan/env" not in statuses
    assert key_file.exists()

    statuses = uninstall.run(repo, dry_run=False, purge_key=True)
    assert statuses["~/.config/flightplan/env"] == "removed"
    assert not key_file.exists()


def test_legacy_hook_wiring_removed(tmp_path):
    """A legacy install may be uninstalled directly, with no upgrade install
    in between — the old command variants must be stripped too."""
    _git_init(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [
            {"type": "command",
             "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/intent_registry_stop_hook.py"'},
            {"type": "command",
             "command": "python3 scripts/stop_hook.py"},
            {"type": "command", "command": "echo user-hook"},
        ]}]},
    }))

    statuses = uninstall.run(tmp_path, dry_run=False)

    assert statuses[".claude/settings.json"] == "updated"
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    commands = [
        h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]
    ]
    assert commands == ["echo user-hook"]


def test_orphaned_marker_left_alone(tmp_path):
    _git_init(tmp_path)
    content = f"# Repo\n\n{install.BEGIN_MARKER}\nno end marker here\n"
    (tmp_path / "CLAUDE.md").write_text(content)

    statuses = uninstall.run(tmp_path, dry_run=False)

    # Without a matching end marker the block's extent is unknown — keep the file.
    assert statuses["CLAUDE.md"] == "absent"
    assert (tmp_path / "CLAUDE.md").read_text() == content


def test_cli_dispatches_uninstall(tmp_path, monkeypatch, capsys):
    from flightplan import cli

    _git_init(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["uninstall", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "uninstall (dry run)" in out
