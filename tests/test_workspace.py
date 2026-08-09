"""Project pins: one workspace folder binding several repositories.

The workspace root replaces the git root for path canonicalization, and each
canonical value is mapped back to the child repo it lives in. What that buys is
on the wire: a post whose work sits in one repo goes out as a plain repository
post, and a wider one carries the per-repository split. Child repos here are
real git repos, because the point of the last test is that a file reaches the
registry as one string from inside a repo and from the workspace above it.
"""

import subprocess
from pathlib import Path

import pytest

from flightplan import config, mcp_server, paths, workspace

PROJECT_PIN = (
    'target = "project"\n'
    'target_id = "proj_5b71ee"\n'
    'name = "flightplan"\n'
    'url = "https://api.getflightplan.com"\n'
)

SERVICE_ID = "repo_aaa111"
CLIENT_ID = "repo_bbb222"

# The file the last test follows from three vantage points.
CANONICAL = "service/intent-registry/src/x.py"
LOCAL = "intent-registry/src/x.py"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo_pin(target_id: str, name: str) -> str:
    return f'target = "repository"\ntarget_id = "{target_id}"\nname = "{name}"\n'


def _child(ws: Path, dirname: str, pin: str, files: tuple[str, ...] = ()) -> Path:
    d = ws / dirname
    d.mkdir(parents=True, exist_ok=True)
    for rel in files:
        path = d / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n")
    (d / ".flightplan.toml").write_text(pin)
    _git("init", "-q", "-b", "main", cwd=d)
    return d


@pytest.fixture
def ws(tmp_path, monkeypatch) -> Path:
    """A workspace with two pinned repos, one legacy-pinned repo, one unpinned
    directory, and a file of its own. Cwd starts at the workspace root."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".flightplan.toml").write_text(PROJECT_PIN)
    (root / "CLAUDE.md").write_text("workspace notes\n")
    _child(root, "service", _repo_pin(SERVICE_ID, "service"), ("intent-registry/src/x.py",))
    _child(root, "client", _repo_pin(CLIENT_ID, "client"), ("src/cli.py",))
    _child(root, "old", 'repo = "old"\n')          # legacy pin: no id to map to
    (root / "notes").mkdir()                        # no pin at all
    monkeypatch.chdir(root)
    return root


def _bound(ws: Path) -> workspace.Workspace:
    pin, bound = workspace.bind(ws)
    assert bound is not None and pin.target == "project"
    return bound


# --- discovery -------------------------------------------------------------

def test_only_pinned_child_repositories_are_discovered(ws):
    children = _bound(ws).children
    assert set(children) == {"service", "client"}
    assert children["service"].target_id == SERVICE_ID
    assert children["client"].name == "client"


def test_a_repository_pin_is_not_a_workspace(tmp_path, monkeypatch):
    # The nearest pin wins: a session inside a child repo never sees the
    # workspace, so nothing about its behaviour changes.
    (tmp_path / ".flightplan.toml").write_text(_repo_pin(SERVICE_ID, "service"))
    monkeypatch.chdir(tmp_path)
    pin, bound = workspace.bind()
    assert bound is None and pin.target_id == SERVICE_ID


def test_no_pin_is_not_a_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert workspace.bind() == (config.EMPTY, None)


# --- mapping ---------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("service", ("service", "**")),                  # the whole repo
    ("service/**", ("service", "**")),               # the same, written out
    ("service/src/api.py", ("service", "src/api.py")),
    ("service/src/*.py", ("service", "src/*.py")),
    ("service/**/*.py", ("service", "**/*.py")),
    ("client/src/cli.py", ("client", "src/cli.py")),
    ("**/*.py", (None, "**/*.py")),                  # no leading segment
    ("*/src", (None, "*/src")),
    ("CLAUDE.md", (None, "CLAUDE.md")),              # a workspace-root file
    ("notes/plan.md", (None, "notes/plan.md")),      # no pin under it
    ("old/thing.py", (None, "old/thing.py")),        # legacy pin: no id
])
def test_mapping(ws, value, expected):
    assert _bound(ws).map_value(value) == expected


def test_grouping_keeps_order_and_separates_the_unmappable(ws):
    mapped, unmapped = _bound(ws).group(
        ["service/a.py", "CLAUDE.md", "client/b.py", "service/c.py"]
    )
    assert list(mapped) == ["service", "client"]
    assert mapped["service"] == ["a.py", "c.py"]
    assert unmapped == ["CLAUDE.md"]


# --- posting ---------------------------------------------------------------

def _post(call, **kwargs) -> None:
    call(mcp_server.post_intent(
        repo=kwargs.pop("repo", "flightplan"),
        summary=kwargs.pop("summary", "Work."),
        **kwargs,
    ))


def test_work_in_one_repository_posts_as_that_repository(ws, registry, call):
    # Nothing on the wire says a workspace was involved: same target, same
    # repo-local paths an agent inside that repo would have sent.
    _post(call, touches=["service/src/api.py", "service/tests/*.py"])
    body = registry.body
    assert body["target_id"] == SERVICE_ID
    assert body["repo"] == "service"
    assert body["touches"] == ["src/api.py", "tests/*.py"]
    assert "repositories" not in body


def test_work_across_repositories_posts_at_the_project_with_a_split(ws, registry, call):
    _post(call, touches=["service/src/api.py", "client/src/cli.py"])
    body = registry.body
    assert body["target_id"] == "proj_5b71ee"
    assert body["repo"] == "flightplan"
    assert body["touches"] == ["service/src/api.py", "client/src/cli.py"]
    assert body["repositories"] == [
        {"target_id": SERVICE_ID, "touches": ["src/api.py"]},
        {"target_id": CLIENT_ID, "touches": ["src/cli.py"]},
    ]


def test_an_unmappable_touch_forces_project_scope(ws, registry, call):
    # One repo plus a workspace-root file. The file belongs to no repository, so
    # the post cannot become that repository's — and it stays out of the split.
    _post(call, touches=["service/src/api.py", "CLAUDE.md"])
    body = registry.body
    assert body["target_id"] == "proj_5b71ee"
    assert body["touches"] == ["service/src/api.py", "CLAUDE.md"]
    assert body["repositories"] == [{"target_id": SERVICE_ID, "touches": ["src/api.py"]}]


def test_a_pure_pattern_keeps_a_post_at_the_project(ws, registry, call):
    _post(call, touches=["**/*.py"])
    body = registry.body
    assert body["target_id"] == "proj_5b71ee"
    assert body["touches"] == ["**/*.py"]
    assert "repositories" not in body


def test_a_whole_repository_touch_demotes(ws, registry, call):
    _post(call, touches=["service"])
    body = registry.body
    assert body["target_id"] == SERVICE_ID
    assert body["touches"] == ["**"]


def test_a_decision_stays_at_the_project(ws, registry, call):
    # A decision is a record, never work: it never collides, so it is never
    # demoted and never split, even when its touches name one repo.
    _post(call, touches=["service/src/api.py"], kind="decision", outcome="Chose X.")
    body = registry.body
    assert body["target_id"] == "proj_5b71ee"
    assert body["touches"] == ["service/src/api.py"]
    assert "repositories" not in body


def test_no_touches_posts_at_the_project(ws, registry, call):
    _post(call, touches=[])
    body = registry.body
    assert body["target_id"] == "proj_5b71ee"
    assert "repositories" not in body


def test_paths_outside_the_workspace_are_rejected(ws, registry, call):
    outside = str(ws.parent / "elsewhere" / "x.py")
    for value in (outside, "../elsewhere/x.py", "service/../../x.py"):
        result = call(mcp_server.post_intent(
            repo="flightplan", summary="s", touches=[value],
        ))
        assert "repository-relative" in result["error"]
        assert "Nothing was sent" in result["advice"]
    assert registry.paths == []


# --- one file, three vantage points ----------------------------------------

def test_one_file_from_the_workspace_and_from_inside_the_repo(
    ws, registry, call, monkeypatch,
):
    # (a) From the workspace root the file is named through its repo directory.
    # The post is demoted, and the repo-local path is what lands.
    _post(call, touches=[CANONICAL])
    assert registry.body["touches"] == [LOCAL]
    assert registry.body["target_id"] == SERVICE_ID

    # (b) A session in the child repo root finds that repo's own pin, so this is
    # today's git-root behaviour, unchanged — and it produces the same string.
    child = ws / "service"
    monkeypatch.chdir(child)
    assert paths.normalize_all([LOCAL]) == [LOCAL]

    # (c) And from a nested directory inside that repo.
    monkeypatch.chdir(child / "intent-registry")
    assert paths.normalize_all(["src/x.py"]) == [LOCAL]


# --- patching --------------------------------------------------------------

def _fetched(registry, target_id: str) -> None:
    """What GET /intents/{id} comes back with — the routing input for a patch."""
    registry.response = {"id": "3f1a", "target_id": target_id}


def test_update_of_a_demoted_intent_sends_repo_local_paths(ws, registry, call):
    # The client is stateless: it asks what the intent is bound to, finds a
    # repository it knows, and rewrites for it. No split — the intent is not a
    # project intent.
    _fetched(registry, SERVICE_ID)
    call(mcp_server.update_intent(id="3f1a", touches=["service/src/api.py", "service"]))
    assert registry.paths == ["/intents/3f1a", "/intents/3f1a"]
    assert registry.body == {"touches": ["src/api.py", "**"]}


def test_complete_of_a_project_intent_splits_files(ws, registry, call):
    _fetched(registry, "proj_5b71ee")
    call(mcp_server.complete_intent(
        id="3f1a", status="done", outcome="o",
        files=["service/src/api.py", "client/src/cli.py", "CLAUDE.md"],
    ))
    body = registry.body
    assert body["files"] == ["service/src/api.py", "client/src/cli.py", "CLAUDE.md"]
    assert body["repositories"] == [
        {"target_id": SERVICE_ID, "files": ["src/api.py"]},
        {"target_id": CLIENT_ID, "files": ["src/cli.py"]},
    ]


def test_a_value_outside_the_intents_repository_is_refused(ws, registry, call):
    # The intent lives in one repo; these paths do not. Sending them would
    # silently record another repo's files against it, so nothing goes.
    _fetched(registry, SERVICE_ID)
    result = call(mcp_server.complete_intent(
        id="3f1a", status="done", outcome="o",
        files=["service/src/api.py", "client/src/cli.py", "CLAUDE.md"],
    ))
    assert "client/src/cli.py" in result["error"] and "CLAUDE.md" in result["error"]
    assert "Nothing was sent" in result["advice"]
    assert registry.paths == ["/intents/3f1a"]      # the read, and nothing else
    assert registry.bodies == []


def test_an_unreadable_intent_is_an_advisory_error(ws, registry, call):
    # The routing read is the one thing a patch cannot do without. It fails the
    # way everything else here fails: a result, never an exception.
    registry.response = {"id": "3f1a"}              # no target_id to route by
    result = call(mcp_server.update_intent(id="3f1a", touches=["service/src/api.py"]))
    assert "could not read intent 3f1a" in result["error"]
    assert "advisory" in result["advice"]
    assert registry.bodies == []


def test_an_intent_bound_somewhere_else_is_refused(ws, registry, call):
    _fetched(registry, "repo_somewhere_else")
    result = call(mcp_server.update_intent(id="3f1a", touches=["service/src/api.py"]))
    assert "repo_somewhere_else" in result["error"]
    assert registry.bodies == []


def test_a_bare_ttl_renewal_is_still_one_request(ws, registry, call):
    # Nothing to route, so nothing to look up: the heartbeat stays cheap.
    call(mcp_server.update_intent(id="3f1a"))
    assert registry.paths == ["/intents/3f1a"]
    assert registry.bodies == [{}]


def test_clearing_touches_clears_the_split(ws, registry, call):
    # An explicit empty list is an edit, not an omission: it has to route, or
    # the parent loses its touches while the per-repository split lives on.
    _fetched(registry, "proj_5b71ee")
    call(mcp_server.update_intent(id="3f1a", touches=[]))
    assert registry.paths == ["/intents/3f1a", "/intents/3f1a"]
    assert registry.body == {"touches": [], "repositories": []}


def test_clearing_files_clears_the_split(ws, registry, call):
    _fetched(registry, "proj_5b71ee")
    call(mcp_server.complete_intent(id="3f1a", status="done", outcome="o", files=[]))
    assert registry.body["files"] == []
    assert registry.body["repositories"] == []


def test_clearing_touches_on_a_demoted_intent_sends_a_plain_empty_list(ws, registry, call):
    # A demoted intent has no split to clear, so it gets the bare empty list.
    _fetched(registry, SERVICE_ID)
    call(mcp_server.update_intent(id="3f1a", touches=[]))
    assert registry.body == {"touches": []}


# --- reads -----------------------------------------------------------------

def test_list_normalizes_overlaps_against_the_workspace(ws, registry, call):
    from urllib.parse import parse_qs, urlparse

    call(mcp_server.list_intents(overlaps=[str(ws / "service" / "src" / "api.py")]))
    query = parse_qs(urlparse(registry.paths[-1]).query)
    assert query["overlaps"][0] == "service/src/api.py"
