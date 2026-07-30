# FlightPlan

> **Your agents collide. File a FlightPlan.**
>
> Before work begins, each of your agents declares where it's headed and sees
> what's already in flight. They coordinate around conflicts, then leave behind
> what changed and why.
>
> **Advisory, never locking.**
>
> **A byproduct of agent work, not another process to maintain.**

This is the FlightPlan client: a thin CLI (`getflightplan`), the MCP server
your coding agent launches, and an install kit that wires a repo up in one
idempotent command. The hosted service lives at
[getflightplan.com](https://getflightplan.com).

## How it works

1. **File the work.** Before the first edit, your agent posts what it is about
   to do: a one-paragraph summary and glob patterns for the areas it expects
   to touch.
2. **See what's in flight.** The response lists overlapping active work across
   your own parallel sessions and your teammates' agents — including
   uncommitted work Git cannot see — plus recent completed work relevant to
   the task at hand.
3. **Coordinate around conflicts.** Overlaps are advisory. The agent tells you
   who is doing what; you decide whether to narrow, sequence, or proceed.
4. **Debrief what happened.** On completion the agent records what actually
   changed, what surprised it, and what it tried and rejected — context the
   next session (yours or a teammate's) reads before starting.

## Status

The hosted service is in private beta — get on the list at
[getflightplan.com](https://getflightplan.com). The package is not yet on
PyPI; until then, install from source:

```sh
uvx --from git+https://github.com/sledmonkey/getflightplan getflightplan install
```

After PyPI publication this becomes `uvx getflightplan install`.

## Install

From your repo's root:

```sh
uvx --from git+https://github.com/sledmonkey/getflightplan getflightplan install                 # Claude Code
uvx --from git+https://github.com/sledmonkey/getflightplan getflightplan install --agent codex   # Codex
```

One command per repo, safe to re-run. It writes:

- `.flightplan.toml` — pins the repo name every agent posts under, plus the
  registry URL. Committed on purpose; no secrets.
- The agent snippet into `CLAUDE.md` (and/or `AGENTS.md`) between managed
  markers — the behavioral contract your agent follows. Full text below.
- `/registry-digest` — an on-demand "what happened lately" command.
- A session-end stop hook (`.claude/hooks/flightplan_stop_hook.py` plus its
  settings wiring) that reminds the agent to close out open intents.

It then verifies MCP registration and registry reachability and offers to fix
what's missing: prompt once for your API key, register the `flightplan` MCP
server, done. Verification never fails the run — like everything here, it
advises.

## Configuration

- `FLIGHTPLAN_URL` — the registry endpoint (set in the MCP server's env).
- `FLIGHTPLAN_API_KEY` — your key (the MCP server's env; the stop hook also
  reads `~/.config/flightplan/env`).
- `.flightplan.toml` — the per-repo pin: `repo` name and `url`.

## What your agent is told

The installer renders this snippet into your repo, with the repo name pinned.
It is the entire behavioral contract — inspect it before you install it:

## Intent registry

This repo participates in the team intent registry (MCP server: `flightplan`).

- **Before starting non-trivial work**, call `post_intent`. The test: will the
  work change behavior, defaults, or contracts another agent would encounter —
  or, for pure investigation, would the findings save the next agent an hour?
  Yes to either → post; Q&A and typo-level fixes, no. Send
  a one-paragraph summary (what + why), `kind` (`build`, or
  `explore`/`spike` for throwaway investigation), and `touches` globs for the
  areas you expect to change. Keep the returned id for later. For `repo`,
  use the basename of the git origin remote (or the repo root directory name if there is no remote) — every agent on this repo must use the same name or
  collision checks silently miss each other. The response may include
  `context`: recent completed work relevant to your task — read those outcomes
  before starting; the surprises and dead ends in them are load-bearing.
- If the response includes overlaps at level `warn`, check what the overlap is
  before pausing. Two cases need no confirmation — mention the overlap and keep
  going: the overlapping intent is the very work you were asked to act on
  (reviewing it, verifying it, following up on it), or your task is read-only.
  Otherwise, tell your user who is doing what and which globs collide, and ask
  how to proceed before continuing. `fyi`/`nudge` levels: mention briefly and
  keep going.
- **If the work changes shape or runs long**, call `update_intent`: revise the
  summary/touches when scope grows (collision checks run against them — stale
  globs miss real collisions), or call with just the id to renew the TTL on
  work spanning more than a day. The response includes fresh `overlaps` — the
  same collision check as posting, glob-based — and a `warn` there gets the
  same treatment as a `warn` at post time.
- **When the work finishes or is abandoned** — including when the session is
  wrapping up — call `complete_intent` with a one-paragraph outcome: what
  actually changed, anything surprising, approaches tried and rejected, anything
  deliberately left in place. If a `warn` overlap changed how the work went
  (coordinated, narrowed scope, proceeded anyway), say which. Attach the git
  facts you already know: `files` actually changed (`git diff --name-only`),
  `commits` created, and `uncommitted: true` if any of the work is not yet
  committed — that flag is what lets other agents' collision checks warn loudly
  instead of quietly. Completing an intent ends the slice, not the session:
  follow-up work after a complete that changes behavior, defaults, or contracts
  gets a fresh post — "same session" doesn't exempt it.
- **Re-check for collisions whenever your picture of in-flight work may be
  stale** — posting checks once, and it goes stale over a long session.
  Re-check moments: a file changed between your read and your edit, or an Edit
  fails on text you just read — someone's work landed under you; before editing
  a shared doc or artifact you didn't create this session; when resuming after
  a handoff from another agent; and before touching files named in an earlier
  `warn`. The cheapest re-check is
  `update_intent` with just your intent id (renews the TTL, returns fresh
  `overlaps`); use `list_intents` (pass `overlaps` globs, plus `summary` for a
  semantic check, or `q`/`since` for history) when you have no open intent or
  are scoping new work.
- **When a decision gets resolved** in conversation (an approach chosen, an
  alternative rejected, a direction set), record it the moment it settles:
  `post_intent` with `kind: "decision"`, the question as the summary, and the
  resolution in `outcome` — what was decided, what was rejected, and why. One
  call; no touches, no completion later. Decisions never collide and become
  searchable team memory. Decisions are also the correction mechanism:
  completed outcomes are immutable, so if one later proves wrong, post a
  decision citing what actually held.
- The registry is advisory and must never block work: if its tools error,
  proceed, and mention the failure to your user once.

## Data

What leaves your machine is the coordination record: intent summaries and
outcome paragraphs, glob patterns, changed-file paths, branch names, and
commit ids — sent only to the registry endpoint you configure. Source code
contents are never uploaded. Everything the registry knows, it learns as a
byproduct of your agents' work.

## License

Apache-2.0
