"""The read tools: fetching one full record, and how much of each row a list
asks for.

Registry responses carry excerpts now, so the wire assertions here are about
what the client asks for — the id it addresses, and whether `detail` goes out
at all. The default is the server's default, so it stays off the wire.
"""

from urllib.parse import parse_qs, urlparse

from flightplan import config, mcp_server

PROJECT_PIN = (
    'target = "project"\n'
    'target_id = "proj_5b71ee"\n'
    'name = "flightplan"\n'
    'url = "https://api.getflightplan.com"\n'
)

CHILD_PIN = 'target = "repository"\ntarget_id = "repo_aaa111"\nname = "service"\n'


def _queries(registry) -> list[dict]:
    return [
        {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}
        for path in registry.paths
    ]


# --- get_intent ------------------------------------------------------------

def test_get_intent_fetches_the_record_by_id(tmp_path, monkeypatch, registry, call):
    monkeypatch.chdir(tmp_path)
    registry.response = {"id": "3f1a9c2b", "outcome": "The whole story."}
    result = call(mcp_server.get_intent(id="3f1a9c2b"))
    assert registry.paths == ["/intents/3f1a9c2b"]
    assert registry.auth == ["Bearer test-key"]
    # Returned as it came: the full record is the point of this tool.
    assert result == {"id": "3f1a9c2b", "outcome": "The whole story."}


def test_get_intent_passes_a_prefix_through_verbatim(tmp_path, monkeypatch, registry, call):
    # The server resolves a unique 8-char prefix. The client does not expand it.
    monkeypatch.chdir(tmp_path)
    call(mcp_server.get_intent(id="3f1a9c2b"[:8]))
    assert registry.paths == ["/intents/3f1a9c2b"]


def test_get_intent_on_a_miss_is_an_advisory_error(tmp_path, monkeypatch, registry, call):
    monkeypatch.chdir(tmp_path)
    registry.status = 404
    registry.response = {"detail": "no such intent"}
    result = call(mcp_server.get_intent(id="nope"))
    assert "(404)" in result["error"] and "no such intent" in result["error"]


# --- list_intents detail ---------------------------------------------------

def test_list_sends_no_detail_by_default(tmp_path, monkeypatch, registry, call):
    # "compact" is the server default too, so the wire stays clean — same rule
    # as `match`.
    monkeypatch.chdir(tmp_path)
    call(mcp_server.list_intents())
    assert "detail" not in _queries(registry)[0]


def test_list_sends_detail_full_when_asked(tmp_path, monkeypatch, registry, call):
    monkeypatch.chdir(tmp_path)
    call(mcp_server.list_intents(detail="full"))
    assert _queries(registry)[0]["detail"] == "full"


def test_every_query_of_a_fan_out_carries_detail(tmp_path, monkeypatch, registry, call):
    # A fan-out merges the answers into one list, so the child queries must ask
    # for the same shape the project query asks for.
    root = tmp_path / "workspace"
    child = root / "service"
    child.mkdir(parents=True)
    (root / config.PIN_FILENAME).write_text(PROJECT_PIN)
    (child / config.PIN_FILENAME).write_text(CHILD_PIN)
    monkeypatch.chdir(root)

    call(mcp_server.list_intents(overlaps=["service/src/api.py"], detail="full"))
    queries = _queries(registry)
    assert len(queries) == 2
    assert all(q["detail"] == "full" for q in queries)
