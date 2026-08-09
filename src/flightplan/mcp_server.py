"""MCP server (stdio) exposing the flightplan MCP tools.

The tool descriptions are the adoption mechanism — agents decide when to call
based on them (SPEC: "MCP tools"). Keep them sharp.

Design rule carried into error handling: the registry is advisory and must
never block work. Network/server failures come back as a result the agent can
read and move past, not an exception that derails the session.

Two things happen here that the tool schemas don't show, because agents should
not have to think about either: the `.flightplan.toml` pin decides what a call
posts and queries under (its id, and its readable name in `repo`), and every
path or glob is canonicalized to repo-relative before it goes on the wire. A
path that resolves outside the repository is refused rather than sent — see
paths.py.

A project pin (`target = "project"`) adds a third thing, still invisible to the
agent: paths are canonicalized against the workspace root and then mapped back
to the child repositories pinned under it (workspace.py). Work that lands in
exactly one child posts as an ordinary repository intent — same target, same
repo-local paths an agent inside that repo would send. Work that spans more
posts under the project and carries a `repositories` field: the same paths,
split per repository, so cross-repository collision checks still line up. That
field is the only wire addition; every other pin shape sends what it always
sent.

Env: FLIGHTPLAN_URL, FLIGHTPLAN_API_KEY.
"""

import os
import uuid
from pathlib import Path
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import config, paths, workspace

# One MCP server process ≈ one agent session; good enough for the spec's
# "agent session id if available".
SESSION = uuid.uuid4().hex[:12]

Kind = Literal["build", "explore", "spike", "decision"]

mcp = FastMCP(
    "flightplan",
    instructions=(
        "Team intent registry. Before starting any non-trivial coding task, call "
        "post_intent; when the work finishes or is abandoned, call complete_intent "
        "with an outcome paragraph. Collision checks go stale over a long session: "
        "before editing a shared doc or artifact you did not create this session, "
        "or when resuming after a handoff, re-check — call update_intent with your "
        "intent id (renews the TTL and returns fresh overlaps) or list_intents. "
        "The registry is advisory: if its tools fail, proceed with your work and "
        "mention the failure to your user once."
    ),
)


def _client() -> httpx.AsyncClient:
    url = os.environ.get("FLIGHTPLAN_URL", "").strip()
    key = os.environ.get("FLIGHTPLAN_API_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "flightplan MCP server is not configured: set FLIGHTPLAN_URL "
            "and FLIGHTPLAN_API_KEY in the MCP server's env (see the FlightPlan README)."
        )
    return httpx.AsyncClient(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {key}"},
        # The post path runs the judge synchronously, worst case two model calls
        # (primary + fallback) at ~8s each before it degrades to globs — so 10s
        # would time out the client before the server's own budget elapses.
        # 45s is generous headroom; the advisory error path handles anything past it.
        timeout=45,
    )


async def _call(method: str, path: str, **kwargs) -> dict:
    try:
        async with _client() as client:
            resp = await client.request(method, path, **kwargs)
    except RuntimeError as e:
        return {"error": str(e)}
    except httpx.HTTPError as e:
        return {
            "error": f"intent registry unreachable ({type(e).__name__}: {e}).",
            "advice": (
                "The registry is advisory — proceed with your work. Mention to "
                "your user once that the intent could not be recorded. Do not retry "
                "more than once."
            ),
        }
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        return {"error": f"registry rejected the request ({resp.status_code}): {detail}"}
    # Returned as-is: server responses may carry fields this client has never
    # heard of, and passing them through is how new ones reach the agent.
    return resp.json()


def _binding() -> tuple[config.Pin, workspace.Workspace | None]:
    """The nearest `.flightplan.toml` pin, read fresh, plus the workspace it
    binds when it is a project pin (None for every other shape).

    Read from the pin file only — the client never asks the service to turn a
    name into an id. The pin wins over whatever an agent passes as `repo`: a
    stale name derived by an agent that never read the pin would otherwise
    route somewhere else.
    """
    return workspace.bind()


def _root(ws: workspace.Workspace | None) -> Path | None:
    """What paths are canonicalized against: the workspace root under a project
    pin, otherwise None, which leaves paths.py to find the git root."""
    return ws.root if ws else None


def _rejected(exc: paths.OutsideRepository) -> dict:
    """A path the client refuses to send, handed back the way every other
    failure is — a result the agent can read and act on."""
    return {
        "error": str(exc),
        "advice": (
            "Nothing was sent. Re-send with repository-relative paths, or omit "
            "the ones that live outside this repository."
        ),
    }


def _target_id_of(payload: dict) -> str | None:
    """The target id out of a fetched intent, tolerating either a bare intent
    object or one inside an envelope. None means we could not read it."""
    if not isinstance(payload, dict) or "error" in payload:
        return None
    inner = payload.get("intent")
    intent = inner if isinstance(inner, dict) else payload
    target_id = intent.get("target_id") if isinstance(intent, dict) else None
    return target_id if isinstance(target_id, str) else None


def _route_post(body: dict, ws: workspace.Workspace, touches: list[str] | None) -> None:
    """Aim a workspace post at the right target, editing `body` in place.

    Work that lands in one repository is ordinary repository work that happened
    to start from the workspace root, so it posts as exactly that — nothing on
    the wire says a workspace was involved. Anything wider stays a project post
    and carries the split, so each repository's own agents still match against
    it. Values that belong to no repository (root files, pure patterns) are why
    a post stays project-scoped, and they travel only in `touches`.
    """
    mapped, unmapped = ws.group(touches)
    if len(mapped) == 1 and not unmapped:
        child = ws.children[next(iter(mapped))]
        body["repo"] = child.name or body["repo"]
        body["target_id"] = child.target_id
        body["touches"] = next(iter(mapped.values()))
    elif mapped:
        body["repositories"] = ws.split(touches, "touches")


async def _route_patch(
    id: str,
    body: dict,
    ws: workspace.Workspace,
    field: str,
    values: list[str],
) -> dict:
    """Send a workspace patch under whatever the intent is actually bound to.

    The client keeps no state, so it cannot know whether this intent was posted
    as project work or demoted into one repository — it asks. The difference
    matters: a demoted intent stores repo-local paths, and workspace-relative
    ones would quietly never match it.

    An empty `values` list is a real edit, not a no-op: it flows through as an
    empty repo-local list, or as `repositories: []` for a project intent, which
    is how the registry learns the old per-repository split is gone.
    """
    fetched = await _call("GET", f"/intents/{id}")
    target_id = _target_id_of(fetched)
    if target_id is None:
        detail = fetched.get("error") if isinstance(fetched, dict) else None
        return {
            "error": (
                f"could not read intent {id}, so this update could not be aimed "
                "at the right repository" + (f": {detail}" if detail else ".")
            ),
            "advice": (
                "Nothing was sent. Check the id you were given at post time. The "
                "registry is advisory — proceed with your work either way."
            ),
        }

    child = ws.child_by_id(target_id)
    if child is not None:
        local: list[str] = []
        outside: list[str] = []
        for value in values:
            child_dir, rewritten = ws.map_value(value)
            if child_dir == child.dir:
                local.append(rewritten)
            else:
                outside.append(value)
        if outside:
            return {
                "error": (
                    f"this intent is recorded in {child.name or child.dir}, and "
                    "these values are outside it: " + ", ".join(outside) + "."
                ),
                "advice": (
                    "Nothing was sent. Re-send only what is under "
                    f"{child.dir}/, or post a separate intent for the work in "
                    "the other repositories."
                ),
            }
        body[field] = local
        return await _call("PATCH", f"/intents/{id}", json=body)

    if target_id != ws.pin.target_id:
        return {
            "error": (
                f"intent {id} belongs to {target_id}, which is neither this "
                "workspace nor a repository pinned under it."
            ),
            "advice": (
                "Nothing was sent. Run the update from the repository the "
                "intent was posted in."
            ),
        }

    body["repositories"] = ws.split(values, field)
    return await _call("PATCH", f"/intents/{id}", json=body)


@mcp.tool(
    description=(
        "Register what you are about to work on so other developers' agents can "
        "avoid collisions. Call this before starting any non-trivial coding task "
        "(anything touching more than a trivial fix). Infer `kind`: `build` for "
        "work meant to land, `explore`/`spike` for throwaway investigation, "
        "`decision` for a resolved decision worth recording (post it the moment a "
        "debate settles: pass the resolution in `outcome` — what was decided, "
        "what was rejected, and why; no `touches` needed; it is stored complete, "
        "never collides, and needs no complete_intent). Infer "
        "`touches` from your plan as repo-relative glob patterns. Returns the "
        "intent id — keep it to post the outcome later. Also returns any "
        "overlapping in-flight intents — active work (alert `warn`/`nudge`/`fyi`) "
        "and recently-completed work that may not have landed in git yet (always "
        "`fyi`): overlaps are the COLLISION signal — if overlap level is `warn`, "
        "tell your user before proceeding; for `fyi`, check whether that work is "
        "already in your tree before redoing it. The response also includes "
        "`context` — recently-completed work relevant to THIS task: read those "
        "outcomes before you start, the surprises and dead ends in them are "
        "load-bearing (a rejected approach you might retry, a gotcha you will hit)."
    )
)
async def post_intent(
    repo: Annotated[str, Field(description="Repository name, e.g. 'raveneye'. Use the basename of the git origin remote (or the repo root directory name if there is no remote) — every agent on the same repo must derive the same string or collision checks silently miss each other.")],
    summary: Annotated[str, Field(description="One paragraph: what you're doing and why.")],
    touches: Annotated[
        list[str],
        Field(description="Repo-relative glob patterns you expect to touch, e.g. ['central/services/scorecard*']."),
    ],
    kind: Annotated[Kind, Field(description="build = meant to land; explore/spike = throwaway investigation; decision = a resolved decision recorded for the feed (requires `outcome`).")] = "build",
    branch: Annotated[str | None, Field(description="Git branch, if known.")] = None,
    outcome: Annotated[
        str | None,
        Field(description="kind=decision only: the resolution — what was decided, what was rejected, and why. Other kinds write outcomes at completion instead."),
    ] = None,
    title: Annotated[
        str | None,
        Field(description="Short headline for the work, ≤80 chars, like a commit subject line (e.g. 'FTS5 search + recall mode'). Cheap to write and the feed reads far better with one — provide it."),
    ] = None,
) -> dict:
    # The pin decides what this posts under: its id when there is one, and its
    # readable name in `repo` either way.
    pin, ws = _binding()
    try:
        touches = paths.normalize_all(touches, _root(ws))
    except paths.OutsideRepository as e:
        return _rejected(e)

    body: dict = {
        "repo": pin.name or repo,
        "summary": summary,
        "touches": touches,
        "kind": kind,
        "branch": branch,
        "session": SESSION,
    }
    if pin.target_id:
        body["target_id"] = pin.target_id
    if outcome is not None:
        body["outcome"] = outcome
    if title is not None:
        body["title"] = title
    # A decision is a record, not work: it never collides, so it is never
    # demoted and never split — it stays at the workspace it was decided in.
    if ws is not None and kind != "decision":
        _route_post(body, ws, touches)
    return await _call("POST", "/intents", json=body)


@mcp.tool(
    description=(
        "Query in-flight and recent work across the team. Three distinct uses — "
        "pick exactly one: (1) pre-planning semantic check — pass `summary` (and "
        "optionally `overlaps` globs) to get judge-assessed semantic overlap before "
        "you post_intent; this is the strong collision check; (2) fast glob "
        "collision check — pass `overlaps` alone (no `summary`, no `q`) for "
        "deterministic prefix matching; (3) context search — pass `q` and `since` "
        "(add `match=any` for recall if a precise query returns nothing) to search "
        "summaries and outcomes including completed work. `q` and `overlaps` are "
        "AND-combined: a descriptive `q` alongside `overlaps` filters out "
        "overlapping intents whose summaries don't contain your words — for a "
        "collision check, omit `q`. `q` matches per-word (all words must appear, "
        "any order). Each returned intent carries an alert_level when `overlaps` is "
        "given: warn = surface loudly to your user; fyi = quiet mention; nudge = "
        "possible duplicate spike, suggest comparing notes."
    )
)
async def list_intents(
    repo: Annotated[str | None, Field(description="Filter to one repository. Use the basename of the git origin remote (or the repo root directory name if there is no remote) — must match the name used in post_intent, or the filter silently returns nothing.")] = None,
    status: Annotated[
        str | None,
        Field(description="Comma-separated of: active, done, abandoned, expired. Omit for all (history included)."),
    ] = None,
    overlaps: Annotated[
        list[str] | None,
        Field(description="Globs you expect to touch; filters to overlapping intents and computes alert levels."),
    ] = None,
    summary: Annotated[
        str | None,
        Field(description="Your planned task, one paragraph. Provide it to get semantic (judge) collision assessment instead of glob-prefix matching — use for a pre-planning check before you're ready to post_intent."),
    ] = None,
    q: Annotated[str | None, Field(description="Plain-text search over summaries and outcomes.")] = None,
    since: Annotated[str | None, Field(description="ISO-8601 timestamp or shorthand like '24h', '7d'.")] = None,
    kind: Annotated[Kind | None, Field(description="Filter results by kind.")] = None,
    my_kind: Annotated[Kind, Field(description="The kind of YOUR planned work; sets alert levels.")] = "build",
    branch: Annotated[
        str | None,
        Field(description="YOUR git branch. Overlaps on the same branch are flagged `same_branch` — your own line of work, likely already in your tree, but verify (it may be uncommitted in another session)."),
    ] = None,
    author: Annotated[
        str | None,
        Field(description="Filter to one person's intents, e.g. 'sarah' — for questions like 'what did Sarah's agent work on last week?'."),
    ] = None,
    session: Annotated[
        str | None,
        Field(description="Filter to one agent session. Pass 'current' for this session's own intents — e.g. to find your still-open intent before wrapping up. Any other value passes through verbatim."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    match: Annotated[
        Literal["all", "any"],
        Field(
            description=(
                "How q terms combine: all (default) = every word must match — "
                "precise; any = recall mode, use when a context search with several "
                "descriptive words comes back empty."
            )
        ),
    ] = "all",
) -> dict:
    # Reads are not routed like posts. `repo` is passed through exactly as the
    # agent asked — naming another repository is a legitimate history query.
    # The pinned id is added only when the query names this checkout's own
    # repo, where it is pure precision. No repo, no filter: the query stays as
    # broad as it was written.
    #
    # Known gap under a project pin: a query scoped to the workspace sees
    # project-scoped intents only, so the pre-planning check here can miss work
    # posted inside a child repository. The post path does the full
    # cross-repository check, which is where a real collision surfaces.
    pin, ws = _binding()
    try:
        overlaps = paths.normalize_all(overlaps, _root(ws))
    except paths.OutsideRepository as e:
        return _rejected(e)

    params: dict = {"my_kind": my_kind, "limit": limit}
    if repo:
        params["repo"] = repo
        if pin.target_id and repo == pin.name:
            params["target_id"] = pin.target_id
    if status:
        params["status"] = status
    if overlaps:
        params["overlaps"] = ",".join(overlaps)
    if summary:
        params["summary"] = summary
    if q:
        params["q"] = q
    if since:
        params["since"] = since
    if kind:
        params["kind"] = kind
    if branch:
        params["branch"] = branch
    if author:
        params["author"] = author
    if session:
        # "current" is a client-side alias for this session's own id — the agent
        # doesn't need to know the actual SESSION value to query its own intents.
        params["session"] = SESSION if session == "current" else session
    # Only forward match when non-default (item 14): "all" is the server default
    # too, so omitting it keeps the wire clean and avoids surprises on older servers.
    if match == "any":
        params["match"] = match
    return await _call("GET", "/intents", params=params)


@mcp.tool(
    description=(
        "Update an in-progress intent. Call when the work changes shape (revise "
        "summary or touches — collision checks run against these fields, so stale "
        "globs silently miss real collisions) or when work runs long (a call with "
        "just the id renews the TTL heartbeat; active intents expire after ~48h "
        "without one). Calling with just the id ALSO returns fresh `overlaps` — "
        "the cheap mid-session collision re-check, since a post-time check goes "
        "stale over a long session. Treat a `warn` here exactly like a warn at "
        "post time: tell your user before proceeding. Never use this to finish "
        "work — call complete_intent for that."
    )
)
async def update_intent(
    id: Annotated[str, Field(description="The intent id returned by post_intent.")],
    summary: Annotated[str | None, Field(description="Revised one-paragraph summary: what + why.")] = None,
    touches: Annotated[
        list[str] | None,
        Field(description="Revised repo-relative glob patterns. Replaces the existing list — include all globs, not just new ones."),
    ] = None,
    branch: Annotated[str | None, Field(description="Git branch, if it has changed.")] = None,
    title: Annotated[str | None, Field(description="Revised short headline, ≤80 chars.")] = None,
) -> dict:
    _, ws = _binding()
    try:
        touches = paths.normalize_all(touches, _root(ws))
    except paths.OutsideRepository as e:
        return _rejected(e)

    # Omit Nones from the wire — explicit nulls are no-ops server-side anyway,
    # but sending only the provided fields keeps the payload unambiguous.
    body: dict = {}
    if summary is not None:
        body["summary"] = summary
    if touches is not None:
        body["touches"] = touches
    if branch is not None:
        body["branch"] = branch
    if title is not None:
        body["title"] = title
    # An OMITTED list skips routing, so the common TTL heartbeat still costs one
    # request. An explicit empty list is a real edit — it clears the touches, and
    # must route so the per-repository split is cleared with them.
    if ws is not None and touches is not None:
        return await _route_patch(id, body, ws, "touches", touches)
    return await _call("PATCH", f"/intents/{id}", json=body)


@mcp.tool(
    description=(
        "Close out an intent when work finishes or is abandoned. The outcome "
        "summary is required for `done` and is the most valuable artifact this "
        "system produces: write one paragraph covering what actually changed, "
        "anything surprising, approaches tried and rejected, and anything "
        "deliberately left in place. "
        "Gather git facts as exhaust — you already have them at completion time: "
        "`files` = repo-relative paths actually changed (`git diff --name-only` "
        "over the work, committed or not); `commits` = SHAs created for this "
        "work; `uncommitted` = true if ANY of the work is not yet committed "
        "(untracked/unstaged/staged-only) — this flag is what lets other agents' "
        "collision checks warn loudly instead of quietly. Omit anything unknown."
    )
)
async def complete_intent(
    id: Annotated[str, Field(description="The intent id returned by post_intent.")],
    status: Annotated[Literal["done", "abandoned"], Field(description="done = landed; abandoned = stopped without landing.")],
    outcome: Annotated[
        str,
        Field(description="One paragraph: what actually changed, surprises, dead ends, things deliberately left alone."),
    ],
    files: Annotated[
        list[str] | None,
        Field(description="Repo-relative paths actually changed (from `git diff --name-only` over the work). Omit if unknown."),
    ] = None,
    commits: Annotated[
        list[str] | None,
        Field(description="Commit SHAs produced for this work. Omit if unknown."),
    ] = None,
    uncommitted: Annotated[
        bool | None,
        Field(description="True if ANY of the work is not yet committed (untracked/unstaged/staged-only). This flag escalates collision warnings for other agents. False = all committed. Omit if unknown."),
    ] = None,
) -> dict:
    _, ws = _binding()
    try:
        files = paths.normalize_all(files, _root(ws))
    except paths.OutsideRepository as e:
        return _rejected(e)

    body: dict = {"status": status, "outcome": outcome}
    if files is not None:
        body["files"] = files
    if commits is not None:
        body["commits"] = commits
    # uncommitted=False is a real value (all committed), not a no-op — send it
    # when explicitly passed so the registry records the confirmed state.
    if uncommitted is not None:
        body["uncommitted"] = uncommitted
    # Same rule as update_intent: omitted skips routing, explicit empty routes.
    if ws is not None and files is not None:
        result = await _route_patch(id, body, ws, "files", files)
    else:
        result = await _call("PATCH", f"/intents/{id}", json=body)
    # Advisory thin-outcome nudge (M3 item 15). Appended to the response on the
    # client side — the server already accepted the outcome, this never blocks.
    # Only for done: abandoned outcomes are legitimately brief by design.
    # Threshold: 160 stripped chars — dumb and cheap, no LLM, no sentence parsing.
    if (
        "error" not in result
        and status == "done"
        and len(outcome.strip()) < 160
    ):
        result["nudge"] = (
            "Outcome is on the short side. The feed is only as valuable as what's "
            "in it — next completion, aim to cover: what surprised you, approaches "
            "tried and rejected, and anything deliberately left in place. Those "
            "three are what future agents will reach for."
        )
    return result


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
