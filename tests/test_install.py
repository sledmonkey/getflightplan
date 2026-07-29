"""Install-kit tests (ROADMAP 23). No DB: `install.run` is pure filesystem work
over a tmp_path repo. The last group are drift tests against THIS repo — they
assert the README/root-CLAUDE.md/digest-command stay byte-equal to what the
installer would write, and so pass only once the installer has been run here."""

import json
import subprocess
from pathlib import Path

from flightplan import install

GIT_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = GIT_ROOT  # flat public layout: README lives at the repo root


def _git_init(path: Path, origin: str | None = None) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)


def _stop_commands(settings: dict) -> list[str]:
    return [
        h["command"]
        for group in settings["hooks"]["Stop"]
        for h in group["hooks"]
    ]


def test_fresh_install_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("FLIGHTPLAN_URL", raising=False)  # pin-less: hit the default
    _git_init(tmp_path, "https://github.com/acme/coolproject.git")
    statuses = install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)

    assert statuses[".flightplan.toml"] == "written"
    assert statuses["CLAUDE.md"] == "written"
    assert statuses[".claude/commands/registry-digest.md"] == "written"
    assert statuses[".claude/hooks/flightplan_stop_hook.py"] == "written"
    assert statuses[".claude/settings.json"] == "written"
    assert "AGENTS.md" not in statuses

    toml = (tmp_path / ".flightplan.toml").read_text()
    assert 'repo = "coolproject"' in toml            # derived from origin basename
    assert f'url = "{install.DEFAULT_URL}"' in toml   # pin-less first install → public default

    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert install.BEGIN_MARKER in claude_md
    assert install.END_MARKER in claude_md
    assert "use `coolproject` (pinned in `.flightplan.toml`; do not derive it)" in claude_md
    assert install.render_snippet("coolproject") in claude_md

    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert install.STOP_HOOK_COMMAND in _stop_commands(settings)

    assert (tmp_path / ".claude/commands/registry-digest.md").read_text() == \
        install._asset_text("registry-digest.md")
    assert (tmp_path / ".claude/hooks/flightplan_stop_hook.py").read_text() == \
        install._asset_text("stop_hook.py")


def test_idempotent_rerun(tmp_path):
    _git_init(tmp_path, "https://github.com/acme/coolproject.git")
    install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)

    def snapshot() -> dict:
        return {
            p.relative_to(tmp_path): p.read_text()
            for p in tmp_path.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }

    before = snapshot()
    statuses = install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)
    assert set(statuses.values()) == {"unchanged"}
    assert snapshot() == before


def test_heading_migration(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# Repo\n\nIntro line.\n\n"
        "## Intent registry\n\nOld hand-pasted text.\n\n"
        "## Other section\n\nKeep me.\n"
    )
    install.run(tmp_path, agent="claude", repo="myrepo", url=None, dry_run=False)
    md = (tmp_path / "CLAUDE.md").read_text()

    assert "Old hand-pasted text." not in md          # old section replaced
    assert install.render_snippet("myrepo") in md       # under managed markers
    assert "# Repo" in md and "Intro line." in md       # preamble untouched
    assert "## Other section" in md and "Keep me." in md  # sibling untouched
    assert md.index(install.END_MARKER) < md.index("## Other section")


def test_settings_merge_drops_legacy_preserves_rest(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command",
                            "command": "python3 intent-registry/scripts/stop_hook.py"}]},
                {"hooks": [{"type": "command", "command": "echo unrelated"}]},
            ]
        },
        "permissions": {"allow": ["Bash(ls:*)"]},
    }, indent=2))

    install.run(tmp_path, agent="claude", repo="r", url=None, dry_run=False)
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    cmds = _stop_commands(settings)
    assert install.STOP_HOOK_COMMAND in cmds
    assert "python3 intent-registry/scripts/stop_hook.py" not in cmds  # legacy gone
    assert "echo unrelated" in cmds                                    # foreign kept
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}        # rest kept

    install.run(tmp_path, agent="claude", repo="r", url=None, dry_run=False)
    cmds2 = _stop_commands(json.loads((tmp_path / ".claude/settings.json").read_text()))
    assert cmds2.count(install.STOP_HOOK_COMMAND) == 1                 # no duplicate


def test_pre_rename_upgrade_migrates_pin_and_hook(tmp_path):
    # A repo exactly as the pre-rename installer left it: legacy pin file,
    # legacy vendored hook + its settings wiring, legacy snippet markers.
    _git_init(tmp_path, "https://github.com/acme/coolproject.git")
    (tmp_path / ".intent-registry.toml").write_text(
        'repo = "pinned-team-name"\nurl = "https://registry.example"\n'
    )
    (tmp_path / ".claude/hooks").mkdir(parents=True)
    (tmp_path / ".claude/hooks/intent_registry_stop_hook.py").write_text("# old hook\n")
    legacy_cmd = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/intent_registry_stop_hook.py"'
    (tmp_path / ".claude/settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": legacy_cmd}]}]}
    }))
    (tmp_path / "CLAUDE.md").write_text(
        "# Repo\n\n<!-- intent-registry:begin — managed by getflightplan install; "
        "edits inside this block are overwritten -->\nold snippet body\n"
        "<!-- intent-registry:end -->\n"
    )

    statuses = install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)

    # The pin migrated — not re-derived from the origin, not the default url —
    # and the legacy pin file is gone.
    toml = (tmp_path / ".flightplan.toml").read_text()
    assert 'repo = "pinned-team-name"' in toml
    assert 'url = "https://registry.example"' in toml
    assert statuses[".intent-registry.toml"] == "removed"
    assert not (tmp_path / ".intent-registry.toml").exists()

    # Exactly one stop hook remains: the new wiring; the old vendored file is gone.
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert _stop_commands(settings) == [install.STOP_HOOK_COMMAND]
    assert statuses[".claude/hooks/intent_registry_stop_hook.py"] == "removed"
    assert not (tmp_path / ".claude/hooks/intent_registry_stop_hook.py").exists()

    # The snippet block was replaced between the legacy markers, not duplicated.
    md = (tmp_path / "CLAUDE.md").read_text()
    assert md.count("flightplan:begin") == 1
    assert "intent-registry:begin" not in md
    assert "old snippet body" not in md


def test_invalid_settings_left_alone(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text("{ not json ]")
    warnings: list[str] = []
    statuses = install.run(
        tmp_path, agent="claude", repo="r", url=None, dry_run=False, warnings=warnings,
    )
    assert (tmp_path / ".claude/settings.json").read_text() == "{ not json ]"  # untouched
    assert ".claude/settings.json" not in statuses
    assert any("not valid" in w or "not a JSON" in w for w in warnings)
    # The rest of the install still happened.
    assert (tmp_path / ".claude/hooks/flightplan_stop_hook.py").exists()


def test_repo_override_then_sticks(tmp_path):
    _git_init(tmp_path, "https://github.com/acme/coolproject.git")
    install.run(tmp_path, agent="claude", repo="override-name", url=None, dry_run=False)
    assert 'repo = "override-name"' in (tmp_path / ".flightplan.toml").read_text()
    assert "use `override-name` (pinned" in (tmp_path / "CLAUDE.md").read_text()

    # Second run WITHOUT --repo: existing pin wins over derivation.
    statuses = install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)
    assert set(statuses.values()) == {"unchanged"}
    toml = (tmp_path / ".flightplan.toml").read_text()
    assert 'repo = "override-name"' in toml and "coolproject" not in toml


def test_codex_writes_agents_md_only(tmp_path):
    statuses = install.run(tmp_path, agent="codex", repo="cdx", url=None, dry_run=False)
    assert statuses["AGENTS.md"] == "written"
    assert "CLAUDE.md" not in statuses
    assert ".claude/commands/registry-digest.md" not in statuses
    assert ".claude/settings.json" not in statuses
    assert not (tmp_path / ".claude").exists()
    assert install.render_snippet("cdx") in (tmp_path / "AGENTS.md").read_text()
    assert (tmp_path / ".flightplan.toml").exists()


def test_dry_run_writes_nothing(tmp_path):
    statuses = install.run(tmp_path, agent="both", repo="dry", url=None, dry_run=True)
    assert statuses and set(statuses.values()) == {"written"}
    assert not (tmp_path / ".flightplan.toml").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".claude").exists()


def test_custom_url_pinned(tmp_path):
    install.run(tmp_path, agent="claude", repo="r", url="https://intents.example.com",
                dry_run=False)
    assert 'url = "https://intents.example.com"' in (tmp_path / ".flightplan.toml").read_text()


# --- Interactive onboarding (decision 2bdbf56c): main()-level, TTY-gated ---

# A stand-in key; declared once so tests compare against the variable and never
# print a literal that looks like a real secret.
_FAKE_KEY = "fp-fake-onboarding-key-0000"


def _offline(monkeypatch):
    """Stub the reachability probe so verify() never hits the network."""
    monkeypatch.setattr(install, "_reachable", lambda url: (False, url + "/healthz"))


def test_interactive_key_prompt_writes_env_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLIGHTPLAN_API_KEY", raising=False)
    monkeypatch.delenv("FLIGHTPLAN_URL", raising=False)
    _offline(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(install.getpass, "getpass", lambda *a, **k: _FAKE_KEY)
    # No `claude` binary → the registration offer is skipped (no subprocess).
    monkeypatch.setattr(install.shutil, "which", lambda _cmd: None)

    assert install.main([]) == 0

    env_file = home / ".config" / "flightplan" / "env"
    assert env_file.exists()
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert (env_file.parent.stat().st_mode & 0o777) == 0o700
    assert env_file.read_text() == f"FLIGHTPLAN_API_KEY={_FAKE_KEY}\n"


def test_no_input_flag_never_prompts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLIGHTPLAN_API_KEY", raising=False)
    _offline(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("must not prompt")

    monkeypatch.setattr(install.getpass, "getpass", boom)
    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)  # a TTY, but --no-input wins

    assert install.main(["--no-input"]) == 0
    assert not (home / ".config" / "flightplan" / "env").exists()


def test_non_tty_never_prompts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLIGHTPLAN_API_KEY", raising=False)
    _offline(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("must not prompt")

    monkeypatch.setattr(install.getpass, "getpass", boom)
    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)  # piped / CI

    assert install.main([]) == 0


def test_legacy_name_shows_reregister_nudge(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"intent-registry": {"command": "uvx"}}})
    )
    _offline(monkeypatch)

    lines = install.verify(tmp_path, agent="claude", url=install.DEFAULT_URL)
    text = "\n".join(lines)
    assert "legacy name 'intent-registry'" in text     # counts as registered, but nudges
    assert "claude mcp add flightplan" in text
    assert "not found" not in text                      # not the missing-guidance line


def _onboard_repo(tmp_path, monkeypatch):
    """A tmp repo + tmp HOME wired for the interactive onboarding path, offline."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLIGHTPLAN_API_KEY", raising=False)
    monkeypatch.delenv("FLIGHTPLAN_URL", raising=False)
    _offline(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    return home, repo


def _capture_subprocess(monkeypatch):
    """Stub subprocess.run inside install, recording every argv. The stub's
    empty stdout makes the git helpers fall back to their cwd defaults."""
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    return calls


def _mcp_adds(calls) -> list[list[str]]:
    return [c for c in calls if len(c) > 2 and c[1:3] == ["mcp", "add"]]


def test_claude_registration_uses_package_source(tmp_path, monkeypatch):
    _onboard_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(install.getpass, "getpass", lambda *a, **k: _FAKE_KEY)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # [Y/n] default = yes
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    adds = _mcp_adds(calls)
    assert [c[0] for c in adds] == ["claude"]
    cmd = adds[0]
    i = cmd.index("--from")
    assert cmd[i + 1] == install.PACKAGE_SOURCE          # never the bare PyPI name
    assert f"FLIGHTPLAN_URL={install.DEFAULT_URL}" in cmd
    assert f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in cmd


def test_codex_registration_uses_package_source(tmp_path, monkeypatch):
    _onboard_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(install.getpass, "getpass", lambda *a, **k: _FAKE_KEY)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--agent", "codex", "--url", install.DEFAULT_URL]) == 0
    adds = _mcp_adds(calls)
    assert [c[0] for c in adds] == ["codex"]
    cmd = adds[0]
    i = cmd.index("--from")
    assert cmd[i + 1] == install.PACKAGE_SOURCE
    assert f"FLIGHTPLAN_URL={install.DEFAULT_URL}" in cmd


def test_saved_key_enables_later_registration(tmp_path, monkeypatch):
    # A previous install (say, --agent codex) saved the key; env is empty. The
    # later claude install must reuse the saved key, not re-prompt or bail.
    _onboard_repo(tmp_path, monkeypatch)
    install._write_key_file(_FAKE_KEY)  # under the monkeypatched HOME

    def boom(*a, **k):
        raise AssertionError("must not prompt for a key that is already saved")

    monkeypatch.setattr(install.getpass, "getpass", boom)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    adds = _mcp_adds(calls)
    assert len(adds) == 1
    assert f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in adds[0]


def test_agent_both_prompts_for_key_once_and_registers_both(tmp_path, monkeypatch):
    _onboard_repo(tmp_path, monkeypatch)
    prompts: list[int] = []

    def fake_getpass(*a, **k):
        prompts.append(1)
        return _FAKE_KEY

    monkeypatch.setattr(install.getpass, "getpass", fake_getpass)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    monkeypatch.setattr(install.shutil, "which", lambda cmd: f"/bin/{cmd}")
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--agent", "both", "--url", install.DEFAULT_URL]) == 0
    assert len(prompts) == 1
    assert [c[0] for c in _mcp_adds(calls)] == ["claude", "codex"]


def test_pre_publish_guidance_never_bare_uvx(tmp_path, monkeypatch):
    # Until PyPI publication, every emitted uvx line must carry --from; the
    # bare `uvx getflightplan` form would 404 for a stranger.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _offline(monkeypatch)
    text = "\n".join(install.verify(tmp_path, agent="both", url=install.DEFAULT_URL))
    text += install._CLAUDE_REGISTER_GUIDANCE + install._CODEX_REGISTER_GUIDANCE
    assert "uvx getflightplan" not in text
    assert install.PACKAGE_SOURCE in text


def test_reverify_after_registration(tmp_path, monkeypatch, capsys):
    _onboard_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(install.getpass, "getpass", lambda *a, **k: _FAKE_KEY)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    out = capsys.readouterr().out
    assert "registered  flightplan MCP server (claude)" in out
    assert "re-verify:" in out
    assert "start a new agent session" in out


# --- Drift tests against THIS repo (pass only after the installer runs here) ---

def test_readme_contains_manual_snippet():
    assert install.render_snippet(None) in (PKG_DIR / "README.md").read_text()


def test_root_claude_md_contains_pinned_snippet():
    # The pinned name comes from the repo's own pin file, not a hardcode — the
    # drift test survives a deliberate re-pin.
    pin = install._toml_value((GIT_ROOT / ".flightplan.toml").read_text(), "repo")
    assert pin, "repo pin missing from .flightplan.toml"
    assert install.render_snippet(pin) in (GIT_ROOT / "CLAUDE.md").read_text()


def test_root_digest_command_matches_asset():
    installed = (GIT_ROOT / ".claude" / "commands" / "registry-digest.md").read_text()
    assert installed == install._asset_text("registry-digest.md")
