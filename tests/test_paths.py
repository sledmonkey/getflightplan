"""Path canonicalization: the same file, referenced from anywhere in a
repository, has to reach the registry as one string — otherwise collision
checks compare strings that never match.

The interesting cases are a nested cwd and a linked worktree, so the tests use
a real temp repo with a real `git worktree add`.
"""

import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from flightplan import mcp_server, paths

CANONICAL = "pkg/mod.py"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A committed repo, a nested directory inside it, and a linked worktree."""
    root = tmp_path / "main"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n")
    (root / "nested" / "dir").mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)

    worktree = tmp_path / "wt"
    _git("worktree", "add", "-q", str(worktree), "-b", "side", cwd=root)
    return root, root / "nested" / "dir", worktree


# --- the core rule ---------------------------------------------------------

def test_one_file_three_vantage_points(tmp_path, monkeypatch):
    root, nested, worktree = _repo(tmp_path)

    def at(cwd: Path, value: str) -> list[str]:
        monkeypatch.chdir(cwd)
        return paths.normalize_all([value])

    assert at(root, "pkg/mod.py") == [CANONICAL]
    assert at(root, str(root / "pkg" / "mod.py")) == [CANONICAL]
    assert at(nested, "../../pkg/mod.py") == [CANONICAL]
    assert at(nested, str(root / "pkg" / "mod.py")) == [CANONICAL]
    assert at(worktree, "pkg/mod.py") == [CANONICAL]
    assert at(worktree, str(worktree / "pkg" / "mod.py")) == [CANONICAL]


def test_outside_a_repo_everything_passes_through(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    values = ["pkg/mod.py", str(tmp_path / "pkg" / "mod.py"), "**/*.py"]
    assert paths.normalize_all(values) == values


# --- globs -----------------------------------------------------------------

def test_pure_patterns_are_untouched(tmp_path, monkeypatch):
    root, nested, _ = _repo(tmp_path)
    for cwd in (root, nested):
        monkeypatch.chdir(cwd)
        assert paths.normalize_all(["**/*.py", "*.md"]) == ["**/*.py", "*.md"]


def test_only_the_concrete_prefix_of_a_glob_moves(tmp_path, monkeypatch):
    root, nested, worktree = _repo(tmp_path)

    monkeypatch.chdir(root)
    assert paths.normalize_all(["pkg/*"]) == ["pkg/*"]
    assert paths.normalize_all([str(root / "pkg") + "/*.py"]) == ["pkg/*.py"]

    monkeypatch.chdir(worktree)
    assert paths.normalize_all(["pkg/*"]) == ["pkg/*"]

    # From a nested cwd with no `pkg` of its own, the glob reads as
    # repo-relative — which is what agents are asked for — and is left alone.
    monkeypatch.chdir(nested)
    assert paths.normalize_all(["pkg/*"]) == ["pkg/*"]

    # But when the nested directory really does hold a `pkg`, the caller meant
    # that one.
    (nested / "pkg").mkdir()
    assert paths.normalize_all(["pkg/*"]) == ["nested/dir/pkg/*"]
    assert paths.normalize_all(["./pkg/*"]) == ["nested/dir/pkg/*"]


def test_planned_paths_that_do_not_exist_yet_are_left_alone(tmp_path, monkeypatch):
    # `touches` routinely names files the work has not created. Nothing to
    # resolve against, so the value goes as written.
    _root, nested, _ = _repo(tmp_path)
    monkeypatch.chdir(nested)
    assert paths.normalize_all(["pkg/newthing.py"]) == ["pkg/newthing.py"]


def test_paths_outside_the_repo_are_rejected(tmp_path, monkeypatch):
    # There is no second namespace to store these in, so they must not be sent.
    root, nested, _ = _repo(tmp_path)
    outside = str(tmp_path / "elsewhere" / "x.py")

    monkeypatch.chdir(root)
    for value in (outside, "../elsewhere/x.py", "../elsewhere/*.py", "pkg/../../x.py"):
        with pytest.raises(paths.OutsideRepository):
            paths.normalize_all([value])

    monkeypatch.chdir(nested)
    with pytest.raises(paths.OutsideRepository):
        paths.normalize_all(["../../../elsewhere/x.py"])


def test_traversal_after_a_wildcard_is_rejected(tmp_path, monkeypatch):
    # Head resolution stops at the first wildcard, so a `..` past it would ride
    # along unchecked. These have a harmless-looking (or empty) head.
    root, _, _ = _repo(tmp_path)
    monkeypatch.chdir(root)
    for value in ("**/../../outside", "pkg/*/../../x", "*/.."):
        with pytest.raises(paths.OutsideRepository):
            paths.normalize_all([value])


def test_deep_globs_without_traversal_still_pass(tmp_path, monkeypatch):
    root, _, _ = _repo(tmp_path)
    monkeypatch.chdir(root)
    assert paths.normalize_all(["pkg/**/sub/*.py"]) == ["pkg/**/sub/*.py"]
    assert paths.normalize_all(["**/*.py"]) == ["**/*.py"]


def test_rejection_names_every_offender(tmp_path, monkeypatch):
    root, _, _ = _repo(tmp_path)
    monkeypatch.chdir(root)
    outside = str(tmp_path / "elsewhere" / "x.py")
    with pytest.raises(paths.OutsideRepository) as exc:
        paths.normalize_all(["pkg/mod.py", outside, "../escape"])
    message = str(exc.value)
    assert outside in message and "../escape" in message
    assert "pkg/mod.py" not in message          # the good one is not blamed
    assert "repository-relative" in message


def test_repo_root_itself(tmp_path, monkeypatch):
    root, _, _ = _repo(tmp_path)
    monkeypatch.chdir(root)
    assert paths.normalize_all([str(root) + "/*"]) == ["*"]
    assert paths.normalize_all(["./"]) == ["."]


# --- through the MCP tools -------------------------------------------------

def _overlaps_sent(registry) -> list[str]:
    query = parse_qs(urlparse(registry.paths[-1]).query)
    return query["overlaps"][0].split(",")


def test_every_wire_field_is_canonicalized(tmp_path, monkeypatch, registry, call):
    root, nested, worktree = _repo(tmp_path)

    for cwd, value in (
        (root, "pkg/mod.py"),
        (nested, "../../pkg/mod.py"),
        (worktree, str(worktree / "pkg" / "mod.py")),
    ):
        monkeypatch.chdir(cwd)
        registry.bodies.clear()
        registry.paths.clear()

        call(mcp_server.post_intent(
            repo="r", summary="s", touches=[value, "**/*.py"],
        ))
        assert registry.bodies[-1]["touches"] == [CANONICAL, "**/*.py"]

        call(mcp_server.update_intent(id="3f1a", touches=[value]))
        assert registry.bodies[-1]["touches"] == [CANONICAL]

        call(mcp_server.complete_intent(
            id="3f1a", status="done", outcome="o", files=[value],
        ))
        assert registry.bodies[-1]["files"] == [CANONICAL]

        call(mcp_server.list_intents(overlaps=[value]))
        assert _overlaps_sent(registry) == [CANONICAL]


def test_a_rejected_path_is_a_tool_error_and_nothing_is_sent(
    tmp_path, monkeypatch, registry, call,
):
    root, _, _ = _repo(tmp_path)
    monkeypatch.chdir(root)
    outside = str(tmp_path / "elsewhere" / "x.py")

    results = [
        call(mcp_server.post_intent(repo="r", summary="s", touches=[outside])),
        call(mcp_server.update_intent(id="3f1a", touches=["../escape"])),
        call(mcp_server.complete_intent(
            id="3f1a", status="done", outcome="o", files=[outside],
        )),
        call(mcp_server.list_intents(overlaps=[outside])),
    ]
    for result in results:
        assert "repository-relative" in result["error"]
        assert "Nothing was sent" in result["advice"]

    assert registry.paths == [] and registry.bodies == []
