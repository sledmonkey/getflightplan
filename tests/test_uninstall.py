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

    install.run(tmp_path, agents=install.AGENTS, repo=None, url=None, dry_run=False)
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

    install.run(tmp_path, agents=("claude",), repo=None, url=None, dry_run=False)
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
    install.run(tmp_path, agents=("claude",), repo=None, url=None, dry_run=False)
    before = _snapshot(tmp_path)

    statuses = uninstall.run(tmp_path, dry_run=True)

    assert _snapshot(tmp_path) == before
    assert statuses[".flightplan.toml"] == "removed"  # reported, not performed


def test_purge_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    key_file = install._write_key_file("sekret")
    assert key_file.exists()
    blocks = tmp_path / "home" / ".cache" / "flightplan" / "stop_hook_blocks.json"
    blocks.parent.mkdir(parents=True)
    blocks.write_text("{}")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    # Default: the machine-level key and block memory survive an uninstall.
    statuses = uninstall.run(repo, dry_run=False)
    assert "~/.config/flightplan/env" not in statuses
    assert key_file.exists()
    assert blocks.exists()

    statuses = uninstall.run(repo, dry_run=False, purge_key=True)
    assert statuses["~/.config/flightplan/env"] == "removed"
    assert not key_file.exists()
    assert statuses["~/.cache/flightplan/stop_hook_blocks.json"] == "removed"
    assert not blocks.exists()


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


# --- Cursor deregistration: a file edit, not a CLI call ---
#
# Machine-level like the others, so the default is no. Only the flightplan
# entry ever goes; everything else in the file is the user's.

def _cursor_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # No claude/codex binary: the loop above the cursor offer stays quiet.
    monkeypatch.setattr(uninstall.shutil, "which", lambda _cmd: None)
    return home


def _write_cursor(home, data) -> Path:
    path = home / ".cursor" / "mcp.json"
    path.write_text(json.dumps(data))
    return path


def _answer(monkeypatch, reply: str) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: reply)


def test_cursor_default_no_keeps_the_entry(tmp_path, monkeypatch):
    home = _cursor_home(tmp_path, monkeypatch)
    before = {"mcpServers": {"flightplan": {"command": "uvx"}}}
    path = _write_cursor(home, before)
    _answer(monkeypatch, "")  # a bare Enter is no

    uninstall._offer_mcp_removal()

    assert json.loads(path.read_text()) == before


def test_cursor_yes_strips_only_the_flightplan_key(tmp_path, monkeypatch, capsys):
    home = _cursor_home(tmp_path, monkeypatch)
    path = _write_cursor(home, {
        "mcpServers": {
            "flightplan": {"command": "uvx"},
            "other": {"command": "node", "args": ["server.js"]},
        },
        "someOtherSetting": {"keep": True},
    })
    _answer(monkeypatch, "y")

    uninstall._offer_mcp_removal()

    data = json.loads(path.read_text())
    assert "flightplan" not in data["mcpServers"]
    assert data["mcpServers"]["other"] == {"command": "node", "args": ["server.js"]}
    assert data["someOtherSetting"] == {"keep": True}
    assert "removed  flightplan MCP registration (cursor)" in capsys.readouterr().out


def test_cursor_legacy_only_file_still_gets_the_offer(tmp_path, monkeypatch):
    # A file holding only the old server name is still ours to offer on.
    home = _cursor_home(tmp_path, monkeypatch)
    path = _write_cursor(home, {
        "mcpServers": {
            uninstall.LEGACY_SERVER_NAME: {"command": "uvx", "args": ["x"]},
            "other": {"command": "node"},
        },
    })
    _answer(monkeypatch, "y")

    uninstall._offer_mcp_removal()

    servers = json.loads(path.read_text())["mcpServers"]
    assert uninstall.LEGACY_SERVER_NAME not in servers
    assert servers["other"] == {"command": "node"}


def test_cursor_yes_also_removes_a_legacy_named_entry(tmp_path, monkeypatch):
    # A leftover entry under the old server name is ours too — the one
    # consent covers both, and other servers stay.
    home = _cursor_home(tmp_path, monkeypatch)
    path = _write_cursor(home, {
        "mcpServers": {
            "flightplan": {"command": "uvx"},
            uninstall.LEGACY_SERVER_NAME: {"command": "uvx", "args": ["x"]},
            "other": {"command": "node"},
        },
    })
    _answer(monkeypatch, "y")

    uninstall._offer_mcp_removal()

    servers = json.loads(path.read_text())["mcpServers"]
    assert "flightplan" not in servers
    assert uninstall.LEGACY_SERVER_NAME not in servers
    assert servers["other"] == {"command": "node"}


def test_cursor_non_json_file_is_left_alone(tmp_path, monkeypatch, capsys):
    home = _cursor_home(tmp_path, monkeypatch)
    path = home / ".cursor" / "mcp.json"
    broken = "{ this is not json"
    path.write_text(broken)
    # Nothing may prompt: there is no entry we can see to offer.
    _answer(monkeypatch, "y")

    uninstall._offer_mcp_removal()

    assert path.read_text() == broken
    assert "not a JSON object" in capsys.readouterr().out


def test_cursor_without_an_entry_never_asks(tmp_path, monkeypatch):
    home = _cursor_home(tmp_path, monkeypatch)
    _write_cursor(home, {"mcpServers": {"other": {"command": "node"}}})

    def refuse(_prompt):
        raise AssertionError("nothing to remove, so nothing may be asked")

    monkeypatch.setattr("builtins.input", refuse)
    uninstall._offer_mcp_removal()
