"""Install-kit tests (ROADMAP 23). No DB: `install.run` is pure filesystem work
over a tmp_path repo. The last group are drift tests against THIS repo — they
assert the README/root-CLAUDE.md/digest-command stay byte-equal to what the
installer would write, and so pass only once the installer has been run here."""

import json
import subprocess
from pathlib import Path

from flightplan import config, install

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


# --- The pin file: a pinned id survives regeneration ---

_PINNED = (
    "# hand-written header the installer will replace\n"
    'target = "repository"\n'
    'target_id = "repo_9f3c2a"\n'
    'name = "coolproject"\n'
    'url = "https://registry.example"\n'
)


def test_pinned_id_survives_regeneration(tmp_path):
    # The managed file is rewritten every run; an id already in it must come
    # through untouched, along with the target kind and the readable name.
    _git_init(tmp_path, "https://github.com/acme/somethingelse.git")
    (tmp_path / ".flightplan.toml").write_text(_PINNED)

    install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)

    pin = config.read_pin((tmp_path / ".flightplan.toml").read_text())
    assert pin.target == "repository"
    assert pin.target_id == "repo_9f3c2a"
    assert pin.name == "coolproject"          # not re-derived from the origin
    assert pin.url == "https://registry.example"
    assert "somethingelse" not in (tmp_path / ".flightplan.toml").read_text()


def test_pinned_id_rerun_is_idempotent(tmp_path):
    (tmp_path / ".flightplan.toml").write_text(_PINNED)
    install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)
    first = (tmp_path / ".flightplan.toml").read_text()

    statuses = install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)
    assert statuses[".flightplan.toml"] == "unchanged"
    assert (tmp_path / ".flightplan.toml").read_text() == first


def test_project_target_survives_too(tmp_path):
    # A workspace pin binds several repos (workspace.py) and is not the
    # installer's to reshape: reinstalling must not rewrite it to a repo/name
    # shape, re-derive the name, or invent an id.
    (tmp_path / ".flightplan.toml").write_text(
        'target = "project"\ntarget_id = "proj_5b71ee"\nname = "coolproject rewrite"\n'
        'url = "https://registry.example"\n'
    )
    install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)

    text = (tmp_path / ".flightplan.toml").read_text()
    pin = config.read_pin(text)
    assert (pin.target, pin.target_id) == ("project", "proj_5b71ee")
    assert pin.name == "coolproject rewrite"
    assert pin.url == "https://registry.example"
    assert "repo = " not in text

    statuses = install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)
    assert statuses[".flightplan.toml"] == "unchanged"


def test_repo_flag_renames_but_keeps_the_id(tmp_path):
    # `--repo` sets the readable name. The id is not the installer's to change.
    (tmp_path / ".flightplan.toml").write_text(_PINNED)
    install.run(tmp_path, agent="claude", repo="renamed", url=None, dry_run=False)
    pin = config.read_pin((tmp_path / ".flightplan.toml").read_text())
    assert pin.name == "renamed"
    assert pin.target_id == "repo_9f3c2a"


def test_legacy_pin_keeps_the_legacy_shape(tmp_path):
    _git_init(tmp_path, "https://github.com/acme/coolproject.git")
    (tmp_path / ".flightplan.toml").write_text(
        'repo = "pinned-team-name"\nurl = "https://registry.example"\n'
    )
    install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)

    text = (tmp_path / ".flightplan.toml").read_text()
    assert 'repo = "pinned-team-name"' in text
    pin = config.read_pin(text)
    assert (pin.name, pin.target, pin.target_id) == ("pinned-team-name", None, None)


def test_fresh_install_never_invents_an_id(tmp_path):
    _git_init(tmp_path, "https://github.com/acme/coolproject.git")
    install.run(tmp_path, agent="claude", repo=None, url=None, dry_run=False)
    pin = config.read_pin((tmp_path / ".flightplan.toml").read_text())
    assert pin.target_id is None and pin.target is None


# --- Promptless registration (decision 72315903): main()-level ---

# A stand-in key; declared once so tests compare against the variable and never
# print a literal that looks like a real secret.
_FAKE_KEY = "fp-fake-onboarding-key-0000"


def _offline(monkeypatch):
    """Stub the reachability probe so verify() never hits the network."""
    monkeypatch.setattr(install, "_reachable", lambda url: (False, url + "/healthz"))


def test_no_credential_points_to_login(tmp_path, monkeypatch, capsys):
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
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )

    assert install.main([]) == 0

    out = capsys.readouterr().out
    assert "next:" in out
    assert "uvx getflightplan login" in out
    # The pending state reads as progress, not as errors: short ".." lines,
    # no manual mcp-add dump, no loud markers on the login-fixable findings.
    assert "..   claude: not connected yet" in out
    assert "..   stop hook: waiting for a credential" in out
    assert "!!   claude" not in out
    assert "!!   stop hook" not in out
    assert "claude mcp add" not in out
    # No credential was invented, and nothing prompted for one.
    assert not (home / ".config" / "flightplan" / "env").exists()


def test_nothing_ever_prompts(tmp_path, monkeypatch):
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

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)  # a TTY changes nothing

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
    """A tmp repo + tmp HOME with a saved credential, offline — the state a
    machine is in after `getflightplan login`."""
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
    install._write_key_file(_FAKE_KEY)  # under the monkeypatched HOME
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
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    adds = _mcp_adds(calls)
    assert [c[0] for c in adds] == ["claude"]
    cmd = adds[0]
    # Published package: bare `uvx getflightplan mcp`, no redundant --from.
    assert cmd[-3:] == ["uvx", "getflightplan", "mcp"]
    assert "--from" not in cmd
    assert f"FLIGHTPLAN_URL={install.DEFAULT_URL}" in cmd
    assert f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in cmd


def test_custom_source_keeps_from(tmp_path, monkeypatch):
    # A development source still needs `uvx --from`.
    _onboard_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL, "--source", "/src/local"]) == 0
    cmd = _mcp_adds(calls)[0]
    assert cmd[-5:] == ["uvx", "--from", "/src/local", "getflightplan", "mcp"]


def test_codex_registration_uses_package_source(tmp_path, monkeypatch):
    _onboard_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--agent", "codex", "--url", install.DEFAULT_URL]) == 0
    adds = _mcp_adds(calls)
    assert [c[0] for c in adds] == ["codex"]
    cmd = adds[0]
    assert cmd[-3:] == ["uvx", "getflightplan", "mcp"]
    assert "--from" not in cmd
    assert f"FLIGHTPLAN_URL={install.DEFAULT_URL}" in cmd


def test_env_var_never_feeds_registration(tmp_path, monkeypatch):
    # Rotation safety (decision 72315903): a stale token in the environment
    # must not reach the registration; the env file the login wrote wins.
    _onboard_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("FLIGHTPLAN_API_KEY", "fp-stale-env-token")
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    cmd = _mcp_adds(calls)[0]
    assert f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in cmd
    assert "FLIGHTPLAN_API_KEY=fp-stale-env-token" not in cmd


def test_non_tty_still_registers(tmp_path, monkeypatch):
    # CI / piped stdin is not special: the registration has no prompts.
    _onboard_repo(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    assert [c[0] for c in _mcp_adds(calls)] == ["claude"]


def test_agent_both_registers_both(tmp_path, monkeypatch):
    _onboard_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(install.shutil, "which", lambda cmd: f"/bin/{cmd}")
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--agent", "both", "--url", install.DEFAULT_URL]) == 0
    assert [c[0] for c in _mcp_adds(calls)] == ["claude", "codex"]


def test_published_guidance_uses_bare_uvx(tmp_path, monkeypatch):
    # Published: every emitted uvx line is the bare package form, and the
    # pre-publish git URL is gone.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _offline(monkeypatch)
    text = "\n".join(install.verify(tmp_path, agent="both", url=install.DEFAULT_URL))
    text += install._claude_guidance() + install._codex_guidance()
    assert "uvx getflightplan" in text
    assert "git+https" not in text
    assert "--from" not in text
    assert install.PACKAGE_SOURCE == "getflightplan"


def test_absent_binary_reads_skipped(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _offline(monkeypatch)
    monkeypatch.setattr(install.shutil, "which", lambda _cmd: None)

    text = "\n".join(install.verify(tmp_path, agent="both", url=install.DEFAULT_URL))
    assert "claude: Claude Code is not on this machine — skipped" in text
    assert "codex: Codex is not on this machine — skipped" in text
    # No promise the login cannot keep, and no loud marker.
    assert "the login does this" not in text
    assert "!!   claude" not in text and "!!   codex" not in text


def test_repair_runs_before_the_report(tmp_path, monkeypatch, capsys):
    # A credentialed machine with a missing registration: the repair happens
    # first, so the report shows the end state — never a "!!" error that the
    # very next line fixes.
    home, _repo = _onboard_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""

        if cmd[:3] == ["claude", "mcp", "add"]:
            # Behave like the real CLI: write the user-scope entry.
            env = dict(
                pair.split("=", 1)
                for flag, pair in zip(cmd, cmd[1:])
                if flag == "--env"
            )
            tail = cmd[cmd.index("--") + 1:]
            (home / ".claude.json").write_text(json.dumps({
                "mcpServers": {"flightplan": {
                    "command": tail[0], "args": tail[1:], "env": env,
                }}
            }))
        return _Result()

    monkeypatch.setattr(install.subprocess, "run", fake_run)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    out = capsys.readouterr().out
    assert "registered  flightplan MCP server (claude)" in out
    assert out.index("registered") < out.index("verify:")
    assert "ok   claude: flightplan MCP server is registered" in out
    assert "!!   claude" not in out
    assert "claude mcp add flightplan" not in out
    assert "start a new agent session" in out


# --- Stale-registration healing ---

# What a pre-publish install left behind: the right name, the wrong source.
_OLD_SOURCE = "git+https://github.com/sledmonkey/getflightplan"
_STALE_CLAUDE = {
    "mcpServers": {
        "flightplan": {
            "command": "uvx",
            "args": ["--from", _OLD_SOURCE, "getflightplan", "mcp"],
        }
    }
}


def test_stale_claude_registration_is_reregistered(tmp_path, monkeypatch, capsys):
    home, _repo = _onboard_repo(tmp_path, monkeypatch)
    (home / ".claude.json").write_text(json.dumps(_STALE_CLAUDE))
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    out = capsys.readouterr().out
    assert _OLD_SOURCE in out              # says what it points at
    assert "uvx getflightplan mcp" in out  # and what it should point at

    # The old entry is dropped before the new one is added.
    assert ["claude", "mcp", "remove", "flightplan", "--scope", "user"] in calls
    cmd = _mcp_adds(calls)[0]
    assert cmd[-3:] == ["uvx", "getflightplan", "mcp"]


def test_dry_run_makes_no_registration(tmp_path, monkeypatch, capsys):
    home, _repo = _onboard_repo(tmp_path, monkeypatch)
    before = json.dumps(_STALE_CLAUDE)
    (home / ".claude.json").write_text(before)
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--dry-run", "--url", install.DEFAULT_URL]) == 0
    out = capsys.readouterr().out
    assert "(dry run)" in out
    assert _OLD_SOURCE in out
    assert "uvx getflightplan mcp" in out
    assert _mcp_adds(calls) == []
    assert (home / ".claude.json").read_text() == before


def test_custom_source_defines_current(tmp_path, monkeypatch):
    # With --source X, "current" means the registration runs X.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "flightplan": {
                "command": "uvx",
                "args": ["--from", "/src/local", "getflightplan", "mcp"],
                "env": {"FLIGHTPLAN_URL": install.DEFAULT_URL},
            }
        }
    }))
    url = install.DEFAULT_URL
    assert install._claude_registration(
        tmp_path, "/src/local", url).status == install.CURRENT
    assert install._claude_registration(
        tmp_path, "getflightplan", url).status == install.STALE


def _codex_config(home: Path, text: str) -> None:
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(text)


def test_codex_stale_source_detected(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _codex_config(home, (
        "[mcp_servers.flightplan]\n"
        'command = "uvx"\n'
        f'args = ["--from", "{_OLD_SOURCE}", "getflightplan", "mcp"]\n'
    ))
    _offline(monkeypatch)

    reg = install._codex_registration(install.PACKAGE_SOURCE, install.DEFAULT_URL)
    assert reg.status == install.STALE
    assert _OLD_SOURCE in reg.detail

    text = "\n".join(install.verify(tmp_path, agent="codex", url=install.DEFAULT_URL))
    assert "registered but" in text
    assert "not found" not in text


def test_codex_current_source_is_ok(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _codex_config(home, (
        "[mcp_servers.flightplan]\n"
        'command = "uvx"\n'
        'args = ["getflightplan", "mcp"]\n'
        f'env = {{ FLIGHTPLAN_URL = "{install.DEFAULT_URL}" }}\n'
    ))
    reg = install._codex_registration(install.PACKAGE_SOURCE, install.DEFAULT_URL)
    assert reg.status == install.CURRENT


def test_codex_unrelated_mention_is_not_registered(tmp_path, monkeypatch):
    # The old substring check called this registered. It is not.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _codex_config(home, (
        "[mcp_servers.other]\n"
        'command = "uvx"\n'
        'args = ["other-server"]\n'
        'env = { FLIGHTPLAN_URL = "https://api.getflightplan.com" }\n'
    ))
    reg = install._codex_registration(install.PACKAGE_SOURCE, install.DEFAULT_URL)
    assert reg.status == install.MISSING


def test_codex_unparseable_toml_does_not_crash(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _offline(monkeypatch)
    _codex_config(home, "[mcp_servers.flightplan\ncommand = uvx ]]]\n")

    reg = install._codex_registration(install.PACKAGE_SOURCE, install.DEFAULT_URL)
    assert reg.status == install.STALE          # can't read it — assume stale
    assert "unparseable" in reg.detail
    # And verify still produces its lines.
    assert install.verify(tmp_path, agent="codex", url=install.DEFAULT_URL)


def test_codex_unparseable_toml_without_our_server_is_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _codex_config(home, "[mcp_servers.other\nbroken ]]]\n")
    reg = install._codex_registration(install.PACKAGE_SOURCE, install.DEFAULT_URL)
    assert reg.status == install.MISSING


def test_claude_non_uvx_registration_is_stale(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "flightplan": {"command": "python", "args": ["-m", "flightplan.mcp_server"]}
        }
    }))
    reg = install._claude_registration(
        tmp_path, install.PACKAGE_SOURCE, install.DEFAULT_URL)
    assert reg.status == install.STALE
    assert "python -m flightplan.mcp_server" in reg.detail


# --- Argv and url validation (a name-only or source-only check is too loose) ---

def _claude_entry(tmp_path, monkeypatch, entry: dict):
    """Write ~/.claude.json with one flightplan entry and classify it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"flightplan": entry}})
    )
    return install._claude_registration(
        tmp_path, install.PACKAGE_SOURCE, install.DEFAULT_URL)


_GOOD_ENV = {"FLIGHTPLAN_URL": install.DEFAULT_URL}


def test_claude_correct_entry_is_current(tmp_path, monkeypatch):
    reg = _claude_entry(tmp_path, monkeypatch, {
        "command": "uvx", "args": ["getflightplan", "mcp"], "env": _GOOD_ENV,
    })
    assert reg.status == install.CURRENT


def test_claude_missing_subcommand_is_stale(tmp_path, monkeypatch):
    # `uvx getflightplan` runs the CLI's default, not the MCP server.
    reg = _claude_entry(tmp_path, monkeypatch, {
        "command": "uvx", "args": ["getflightplan"], "env": _GOOD_ENV,
    })
    assert reg.status == install.STALE
    assert "uvx getflightplan" in reg.detail


def test_claude_wrong_subcommand_is_stale(tmp_path, monkeypatch):
    reg = _claude_entry(tmp_path, monkeypatch, {
        "command": "uvx", "args": ["getflightplan", "install"], "env": _GOOD_ENV,
    })
    assert reg.status == install.STALE
    assert "install" in reg.detail


def test_claude_wrong_url_is_stale(tmp_path, monkeypatch):
    reg = _claude_entry(tmp_path, monkeypatch, {
        "command": "uvx", "args": ["getflightplan", "mcp"],
        "env": {"FLIGHTPLAN_URL": "http://localhost:8000",
                "FLIGHTPLAN_API_KEY": "fp-should-never-be-printed"},
    })
    assert reg.status == install.STALE
    assert "registered URL http://localhost:8000" in reg.detail
    assert f"expected {install.DEFAULT_URL}" in reg.detail
    assert "fp-should-never-be-printed" not in reg.detail  # keys stay secret


def test_claude_secret_in_argv_is_redacted(tmp_path, monkeypatch):
    # Legacy shape: the key rides in the argv via `env`, not the env dict.
    reg = _claude_entry(tmp_path, monkeypatch, {
        "command": "env",
        "args": ["FLIGHTPLAN_API_KEY=fp-argv-secret",
                 f"FLIGHTPLAN_URL={install.DEFAULT_URL}",
                 "uvx", "getflightplan", "mcp"],
    })
    assert reg.status == install.STALE
    assert "fp-argv-secret" not in reg.detail
    assert "FLIGHTPLAN_API_KEY=[redacted]" in reg.detail
    # Non-secret tokens survive so the user can still see what it runs.
    assert "uvx getflightplan mcp" in reg.detail


def test_claude_missing_env_is_stale(tmp_path, monkeypatch):
    reg = _claude_entry(tmp_path, monkeypatch, {
        "command": "uvx", "args": ["getflightplan", "mcp"],
    })
    assert reg.status == install.STALE
    assert "no FLIGHTPLAN_URL set" in reg.detail


def _codex_entry(tmp_path, monkeypatch, args: str, env: str = "") -> object:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _codex_config(home, (
        "[mcp_servers.flightplan]\n"
        'command = "uvx"\n'
        f"args = [{args}]\n"
        f"{env}"
    ))
    return install._codex_registration(install.PACKAGE_SOURCE, install.DEFAULT_URL)


_CODEX_GOOD_ENV = f'env = {{ FLIGHTPLAN_URL = "{install.DEFAULT_URL}" }}\n'


def test_codex_correct_entry_is_current(tmp_path, monkeypatch):
    reg = _codex_entry(tmp_path, monkeypatch, '"getflightplan", "mcp"',
                       _CODEX_GOOD_ENV)
    assert reg.status == install.CURRENT


def test_codex_missing_subcommand_is_stale(tmp_path, monkeypatch):
    reg = _codex_entry(tmp_path, monkeypatch, '"getflightplan"', _CODEX_GOOD_ENV)
    assert reg.status == install.STALE


def test_codex_wrong_subcommand_is_stale(tmp_path, monkeypatch):
    reg = _codex_entry(tmp_path, monkeypatch, '"getflightplan", "install"',
                       _CODEX_GOOD_ENV)
    assert reg.status == install.STALE
    assert "install" in reg.detail


def test_codex_wrong_url_is_stale(tmp_path, monkeypatch):
    reg = _codex_entry(
        tmp_path, monkeypatch, '"getflightplan", "mcp"',
        'env = { FLIGHTPLAN_URL = "http://localhost:8000" }\n',
    )
    assert reg.status == install.STALE
    assert "registered URL http://localhost:8000" in reg.detail
    assert f"expected {install.DEFAULT_URL}" in reg.detail


def test_codex_missing_env_is_stale(tmp_path, monkeypatch):
    reg = _codex_entry(tmp_path, monkeypatch, '"getflightplan", "mcp"')
    assert reg.status == install.STALE
    assert "no FLIGHTPLAN_URL set" in reg.detail


# --- TOML string escaping (names come from the service, not from us) ---

def test_toml_string_quotes_plain_text():
    assert install._toml_string("widgets") == '"widgets"'


def test_toml_string_escapes_quotes_and_backslashes():
    assert install._toml_string('a"b') == '"a\\"b"'
    assert install._toml_string("a\\b") == '"a\\\\b"'
    # A backslash in front of a quote must not escape our escape.
    assert install._toml_string('a\\"b') == '"a\\\\\\"b"'


def test_toml_string_drops_control_characters():
    assert install._toml_string("a\nb\tc\x00d\x7fe") == '"abcde"'
    assert install._toml_string("plain\x1b[2Ktext") == '"plain[2Ktext"'


def test_toml_string_drops_c1_controls_and_bidi_overrides():
    # U+009B is a one-character CSI: an escape sequence with no ESC byte.
    # Hostile characters are written as escapes on purpose.
    assert install._toml_string("a\u009b2Kb") == '"a2Kb"'
    assert install._toml_string("a\u202eb\u202cc") == '"abc"'
    assert install._toml_string("a\u2066b\u2069c") == '"abc"'
    assert install._toml_string("a\u061cb\u200ec\u200fd") == '"abcd"'


def test_the_unsafe_set_covers_every_cc_character():
    """The pattern uses explicit ranges, so prove they are exactly category
    Cc — plus the bidi set — rather than trusting the comment beside them."""
    import unicodedata

    cc = {c for c in map(chr, range(0x110000)) if unicodedata.category(c) == "Cc"}
    bidi = {
        chr(c)
        for c in [
            0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A),
        ]
    }
    actual = {
        c for c in map(chr, range(0x110000)) if install._UNSAFE_CHARS.fullmatch(c)
    }
    assert actual == cc | bidi


def test_the_unsafe_set_keeps_ordinary_text():
    for text in ("café", "日本語のリポジトリ", "Ünïcodé — v2", "emoji 🚀", "a b"):
        assert install._toml_string(text) == f'"{text}"'


def test_toml_string_output_is_one_line_and_parses():
    import tomllib

    hostile = 'evil"\nurl = "https://attacker.test'
    line = f"name = {install._toml_string(hostile)}"
    assert "\n" not in line
    parsed = tomllib.loads(line)
    assert list(parsed) == ["name"]          # no second key was injected
    assert parsed["name"] == 'evil"url = "https://attacker.test'


def test_write_pin_target_escapes_every_value(tmp_path):
    import tomllib

    install.write_pin_target(
        tmp_path, target="repository", target_id='id"1', name='a"\nb',
    )
    parsed = tomllib.loads(
        (tmp_path / config.PIN_FILENAME).read_text(encoding="utf-8")
    )
    assert parsed["target_id"] == 'id"1'
    assert parsed["name"] == 'a"b'


# --- Drift tests against THIS repo (pass only after the installer runs here) ---

def test_readme_contains_manual_snippet():
    assert install.render_snippet(None) in (PKG_DIR / "README.md").read_text()


def test_root_claude_md_contains_pinned_snippet():
    # The pinned name comes from the repo's own pin file, not a hardcode — the
    # drift test survives a deliberate re-pin.
    pin = config.read_pin((GIT_ROOT / ".flightplan.toml").read_text()).name
    assert pin, "repo pin missing from .flightplan.toml"
    assert install.render_snippet(pin) in (GIT_ROOT / "CLAUDE.md").read_text()


def test_root_digest_command_matches_asset():
    installed = (GIT_ROOT / ".claude" / "commands" / "registry-digest.md").read_text()
    assert installed == install._asset_text("registry-digest.md")


# --- Credential rotation and failed-replacement recovery (review round 2) ---

def _current_claude_entry(key=None, url=None):
    """A ~/.claude.json whose flightplan entry is current except, perhaps,
    for the embedded credential."""
    env = {"FLIGHTPLAN_URL": url or install.DEFAULT_URL}
    if key is not None:
        env["FLIGHTPLAN_API_KEY"] = key
    return {
        "mcpServers": {
            "flightplan": {
                "command": "uvx",
                "args": ["getflightplan", "mcp"],
                "env": env,
            }
        }
    }


def test_matching_credential_is_left_alone(tmp_path, monkeypatch):
    home, _repo = _onboard_repo(tmp_path, monkeypatch)
    (home / ".claude.json").write_text(json.dumps(_current_claude_entry(_FAKE_KEY)))
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    assert _mcp_adds(calls) == []


def test_rotation_replaces_the_embedded_credential(tmp_path, monkeypatch, capsys):
    # A login stored a new credential; the registration still carries the old
    # one. The next run must replace it — and never print either value.
    home, _repo = _onboard_repo(tmp_path, monkeypatch)
    old = "fp-old-rotated-out"
    (home / ".claude.json").write_text(json.dumps(_current_claude_entry(old)))
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    out = capsys.readouterr().out
    assert "a different credential" in out
    assert old not in out
    assert _FAKE_KEY not in out

    assert ["claude", "mcp", "remove", "flightplan", "--scope", "user"] in calls
    cmd = _mcp_adds(calls)[0]
    assert f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in cmd


def test_registration_without_a_credential_is_stale(tmp_path, monkeypatch):
    home, _repo = _onboard_repo(tmp_path, monkeypatch)
    (home / ".claude.json").write_text(json.dumps(_current_claude_entry(None)))
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    cmd = _mcp_adds(calls)[0]
    assert f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in cmd


def test_failed_replacement_restores_the_old_entry(tmp_path, monkeypatch, capsys):
    # The new add fails after the old entry was removed: the old entry is
    # rebuilt from its stored command/args/env and put back.
    home, _repo = _onboard_repo(tmp_path, monkeypatch)
    old = "fp-old-rotated-out"
    (home / ".claude.json").write_text(json.dumps(_current_claude_entry(old)))
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""

        if "add" in cmd and f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in cmd:
            _Result.returncode = 1  # only the replacement add fails
        return _Result()

    monkeypatch.setattr(install.subprocess, "run", fake_run)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    out = capsys.readouterr().out
    assert "mcp add failed" in out
    assert "put back" in out
    assert old not in out  # the restore command is never printed

    restores = [c for c in _mcp_adds(calls) if f"FLIGHTPLAN_API_KEY={old}" in c]
    assert len(restores) == 1
    assert restores[0][:6] == ["claude", "mcp", "add", "flightplan", "--scope", "user"]
    assert restores[0][-3:] == ["uvx", "getflightplan", "mcp"]


def test_project_registration_is_replaced_in_project_scope(
    tmp_path, monkeypatch,
):
    _home, repo = _onboard_repo(tmp_path, monkeypatch)
    old = "fp-old-project-token"
    (repo / ".mcp.json").write_text(json.dumps(_current_claude_entry(old)))
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    assert [
        "claude", "mcp", "remove", "flightplan", "--scope", "project",
    ] in calls
    replacement = next(
        c for c in _mcp_adds(calls) if f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in c
    )
    assert replacement[:6] == [
        "claude", "mcp", "add", "flightplan", "--scope", "project",
    ]


def test_failed_project_replacement_restores_project_scope(
    tmp_path, monkeypatch, capsys,
):
    _home, repo = _onboard_repo(tmp_path, monkeypatch)
    old = "fp-old-project-token"
    (repo / ".mcp.json").write_text(json.dumps(_current_claude_entry(old)))
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""

        if "add" in cmd and f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in cmd:
            _Result.returncode = 1
        return _Result()

    monkeypatch.setattr(install.subprocess, "run", fake_run)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    out = capsys.readouterr().out
    assert "put back" in out
    assert old not in out
    restores = [c for c in _mcp_adds(calls) if f"FLIGHTPLAN_API_KEY={old}" in c]
    assert len(restores) == 1
    assert restores[0][:6] == [
        "claude", "mcp", "add", "flightplan", "--scope", "project",
    ]


def test_local_registration_is_replaced_in_local_scope(tmp_path, monkeypatch):
    home, repo = _onboard_repo(tmp_path, monkeypatch)
    old = "fp-old-local-token"
    (home / ".claude.json").write_text(json.dumps({
        "projects": {str(repo): _current_claude_entry(old)},
    }))
    monkeypatch.setattr(
        install.shutil, "which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None
    )
    calls = _capture_subprocess(monkeypatch)

    assert install.main(["--url", install.DEFAULT_URL]) == 0
    assert [
        "claude", "mcp", "remove", "flightplan", "--scope", "local",
    ] in calls
    replacement = next(
        c for c in _mcp_adds(calls) if f"FLIGHTPLAN_API_KEY={_FAKE_KEY}" in c
    )
    assert replacement[:6] == [
        "claude", "mcp", "add", "flightplan", "--scope", "local",
    ]
