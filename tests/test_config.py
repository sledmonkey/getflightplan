"""The `.flightplan.toml` pin: both shapes parse, and the shape decides whether
a post carries a target id. The wire assertions go through the real MCP tool
functions against a stub registry, so they check what actually gets sent."""

from urllib.parse import parse_qs, urlparse

import pytest

from flightplan import config, mcp_server

LEGACY = (
    "# Managed by getflightplan install.\n"
    'repo = "coolproject"\n'
    'url = "https://api.getflightplan.com"\n'
)

REPOSITORY = (
    "# Managed by getflightplan install.\n"
    'target = "repository"\n'
    'target_id = "repo_9f3c2a"\n'
    'name = "coolproject"\n'
    'url = "https://api.getflightplan.com"\n'
)

PROJECT = (
    'target = "project"\n'
    'target_id = "proj_5b71ee"\n'
    'name = "coolproject rewrite"\n'
    'url = "https://api.getflightplan.com"\n'
)


# --- parsing ---------------------------------------------------------------

def test_legacy_shape():
    pin = config.read_pin(LEGACY)
    assert pin.name == "coolproject"
    assert pin.target_id is None
    assert pin.target is None
    assert pin.url == "https://api.getflightplan.com"


def test_repository_target_shape():
    pin = config.read_pin(REPOSITORY)
    assert pin.name == "coolproject"
    assert pin.target_id == "repo_9f3c2a"
    assert pin.target == "repository"


def test_project_target_shape():
    pin = config.read_pin(PROJECT)
    assert pin.name == "coolproject rewrite"
    assert pin.target_id == "proj_5b71ee"
    assert pin.target == "project"


def test_name_wins_over_repo_when_both_present():
    pin = config.read_pin('repo = "old"\nname = "new"\ntarget_id = "repo_1"\n')
    assert pin.name == "new"


def test_no_pin_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config.find_pin() == config.EMPTY


def test_find_pin_walks_up_from_a_nested_dir(tmp_path, monkeypatch):
    (tmp_path / config.PIN_FILENAME).write_text(REPOSITORY)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert config.find_pin().target_id == "repo_9f3c2a"


# --- what the pin puts on the wire -----------------------------------------

def _post(call, registry, cwd, **kwargs) -> dict:
    call(mcp_server.post_intent(
        repo=kwargs.pop("repo", "coolproject"),
        summary="Refactor the ingest pipeline.",
        touches=kwargs.pop("touches", ["**/*.py"]),
        **kwargs,
    ))
    return registry.body


def _query(registry) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(registry.paths[-1]).query).items()}


def test_legacy_pin_posts_without_target_id(tmp_path, monkeypatch, registry, call):
    (tmp_path / config.PIN_FILENAME).write_text(LEGACY)
    monkeypatch.chdir(tmp_path)
    body = _post(call, registry, tmp_path)
    assert "target_id" not in body
    assert body["repo"] == "coolproject"


def test_repository_pin_posts_target_id(tmp_path, monkeypatch, registry, call):
    (tmp_path / config.PIN_FILENAME).write_text(REPOSITORY)
    monkeypatch.chdir(tmp_path)
    body = _post(call, registry, tmp_path)
    assert body["target_id"] == "repo_9f3c2a"
    assert body["repo"] == "coolproject"        # the readable name still goes


def test_project_pin_posts_target_id(tmp_path, monkeypatch, registry, call):
    (tmp_path / config.PIN_FILENAME).write_text(PROJECT)
    monkeypatch.chdir(tmp_path)
    body = _post(call, registry, tmp_path, repo="coolproject rewrite")
    assert body["target_id"] == "proj_5b71ee"


@pytest.mark.parametrize("pin_text", [LEGACY, REPOSITORY, ""])
def test_only_a_project_pin_changes_the_wire(pin_text, tmp_path, monkeypatch, registry, call):
    # The compatibility bar for project pins (workspace.py): every other shape
    # sends exactly what it sent before — no `repositories` field, and no extra
    # lookup before a patch.
    if pin_text:
        (tmp_path / config.PIN_FILENAME).write_text(pin_text)
    monkeypatch.chdir(tmp_path)

    call(mcp_server.post_intent(repo="coolproject", summary="s", touches=["src/a.py"]))
    call(mcp_server.update_intent(id="3f1a", touches=["src/a.py"]))
    call(mcp_server.complete_intent(
        id="3f1a", status="done", outcome="o", files=["src/a.py"],
    ))

    assert registry.paths == ["/intents", "/intents/3f1a", "/intents/3f1a"]
    assert [b.get("touches") or b.get("files") for b in registry.bodies] == [
        ["src/a.py"], ["src/a.py"], ["src/a.py"],
    ]
    assert all("repositories" not in body for body in registry.bodies)


def test_no_pin_posts_without_target_id(tmp_path, monkeypatch, registry, call):
    monkeypatch.chdir(tmp_path)
    body = _post(call, registry, tmp_path)
    assert "target_id" not in body


def test_target_id_is_never_looked_up_over_the_network(tmp_path, monkeypatch, registry, call):
    # The pin file is the only source. One request goes out, and it is the post.
    (tmp_path / config.PIN_FILENAME).write_text(REPOSITORY)
    monkeypatch.chdir(tmp_path)
    _post(call, registry, tmp_path)
    assert registry.paths == ["/intents"]


def test_pin_overrides_a_stale_agent_supplied_name(tmp_path, monkeypatch, registry, call):
    # An agent that never read the pin derives its own name. The pin wins, so
    # the post can't land somewhere the rest of the team is not looking.
    (tmp_path / config.PIN_FILENAME).write_text(REPOSITORY)
    monkeypatch.chdir(tmp_path)
    body = _post(call, registry, tmp_path, repo="Code")
    assert body["repo"] == "coolproject"
    assert body["target_id"] == "repo_9f3c2a"


def test_agent_repo_is_kept_when_nothing_is_pinned(tmp_path, monkeypatch, registry, call):
    monkeypatch.chdir(tmp_path)
    body = _post(call, registry, tmp_path, repo="derived-name")
    assert body["repo"] == "derived-name"


# --- list_intents: reads are not routed like posts ---
#
# Posting from this checkout always belongs to this checkout's repo. Reading is
# different: naming another repo is a legitimate history query, so the agent's
# `repo` goes through exactly as written and only a query about this checkout's
# own repo gains the pinned id.

def test_list_adds_the_id_when_the_query_names_this_repo(tmp_path, monkeypatch, registry, call):
    (tmp_path / config.PIN_FILENAME).write_text(REPOSITORY)
    monkeypatch.chdir(tmp_path)
    call(mcp_server.list_intents(repo="coolproject"))
    query = _query(registry)
    assert query["repo"] == "coolproject"
    assert query["target_id"] == "repo_9f3c2a"


def test_list_leaves_a_cross_repo_query_alone(tmp_path, monkeypatch, registry, call):
    # "What did the team do in some other repo?" must keep working — no
    # substitution, and no id that would scope it back to this one.
    (tmp_path / config.PIN_FILENAME).write_text(REPOSITORY)
    monkeypatch.chdir(tmp_path)
    call(mcp_server.list_intents(repo="another-repo"))
    query = _query(registry)
    assert query["repo"] == "another-repo"
    assert "target_id" not in query


def test_list_without_a_repo_argument_gains_nothing(tmp_path, monkeypatch, registry, call):
    # A query across every repo stays that way: no repo filter, no id.
    (tmp_path / config.PIN_FILENAME).write_text(PROJECT)
    monkeypatch.chdir(tmp_path)
    call(mcp_server.list_intents())
    query = _query(registry)
    assert "repo" not in query
    assert "target_id" not in query


def test_list_with_a_legacy_pin_sends_no_target_id(tmp_path, monkeypatch, registry, call):
    # Nothing to inject, and the name still goes as the agent wrote it.
    (tmp_path / config.PIN_FILENAME).write_text(LEGACY)
    monkeypatch.chdir(tmp_path)
    for asked in ("coolproject", "another-repo"):
        registry.paths.clear()
        call(mcp_server.list_intents(repo=asked))
        query = _query(registry)
        assert query["repo"] == asked
        assert "target_id" not in query


@pytest.mark.parametrize("pin", [REPOSITORY, LEGACY])
def test_a_check_with_globs_is_one_query_without_a_project_pin(
    tmp_path, monkeypatch, registry, call, pin,
):
    # Only a project pin has child repositories to ask. Every other pin sends
    # the one query it always sent.
    (tmp_path / config.PIN_FILENAME).write_text(pin)
    monkeypatch.chdir(tmp_path)
    call(mcp_server.list_intents(overlaps=["src/api.py", "tests/*.py"]))
    assert len(registry.paths) == 1
    assert _query(registry)["overlaps"] == "src/api.py,tests/*.py"


def test_unknown_response_fields_pass_through(tmp_path, monkeypatch, registry, call):
    # Responses gained a nullable target_id; they will gain more. The client
    # hands the whole object back rather than filtering it.
    monkeypatch.chdir(tmp_path)
    registry.response = {
        "id": "3f1a", "target_id": None, "something_new": {"nested": 1},
    }
    result = call(mcp_server.post_intent(
        repo="coolproject", summary="s", touches=["**/*.py"],
    ))
    assert result["target_id"] is None
    assert result["something_new"] == {"nested": 1}
