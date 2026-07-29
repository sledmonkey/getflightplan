"""Front-door multiplex tests (`getflightplan`). No DB, no network: the CLI is
a thin dispatcher, so we assert it routes to the right subcommand and degrades
cleanly. Heavy modules are imported lazily inside cli.main, so importing cli
here pulls in nothing but the standard library."""

import subprocess
import sys
from pathlib import Path

import pytest

from flightplan import cli, install


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_install_subcommand_dispatches_dry_run(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("FLIGHTPLAN_URL", raising=False)
    # Keep verify offline so the dispatch test never touches the network.
    monkeypatch.setattr(install, "_reachable", lambda url: (False, url + "/healthz"))

    rc = cli.main(["install", "--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "dry run" in out          # the installer's own header
    assert "written" in out          # statuses were computed and printed
    assert not (repo / ".flightplan.toml").exists()   # --dry-run wrote nothing


def test_install_subcommand_forwards_flags(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(install, "_reachable", lambda url: (False, url + "/healthz"))

    # REMAINDER must carry every flag through verbatim: codex-only, dry-run.
    rc = cli.main(["install", "--dry-run", "--agent", "codex"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AGENTS.md" in out        # proves --agent codex reached the installer
    assert "CLAUDE.md" not in out


def test_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "getflightplan" in capsys.readouterr().out
