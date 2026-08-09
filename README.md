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

FlightPlan coordinates coding agents before their work collides. This repo
contains the CLI, MCP server, and installer for the hosted service at
[getflightplan.com](https://getflightplan.com).

## Quick start

From your repo's root:

```sh
uvx getflightplan install
```

That installs FlightPlan for Claude Code. For Codex, append
`--agent codex`. The command is safe to re-run.

The hosted service is in private beta; request access at
[getflightplan.com](https://getflightplan.com). The package is on PyPI, so the
command above is all you need. To pin a branch or commit instead, install from
the source:
`uvx --from git+https://github.com/sledmonkey/getflightplan getflightplan install`.

Version and compatibility policy: [docs/versioning.md](docs/versioning.md).

## How it works

1. **File the work.** Before editing, an agent declares its task and the files
   it expects to touch.
2. **See what's in flight.** FlightPlan returns overlapping active work,
   including uncommitted changes Git cannot see, decisions made during coding,
   plus relevant recent outcomes.
3. **Coordinate.** Overlaps are advisory: narrow the work, sequence it, or
   proceed with context.
4. **Debrief.** The agent records what changed, what surprised it, and what it
   tried so the next session does not start cold.

## What the installer adds

- `.flightplan.toml` — pins the repo name every agent posts under, plus the
  registry URL. Committed on purpose; no secrets.
- A managed agent snippet in `CLAUDE.md` and/or `AGENTS.md`.
- `/registry-digest` — an on-demand "what happened lately" command.
- A session-end stop hook (`.claude/hooks/flightplan_stop_hook.py` plus its
  settings wiring) that reminds the agent to close out open intents.

It also checks MCP registration and service reachability, then offers to
configure anything missing. Verification is advisory and never fails the run.

To remove everything the installer wrote, run `getflightplan uninstall` from
the repo root (`--dry-run` to preview, `--purge-key` to also delete the saved
API key).

## Logging in

`getflightplan login` gets a credential without a copied API key. It opens
your browser, you approve there, and the credential goes to
`~/.config/flightplan/env` with mode 600. The credential is never printed.

On a machine with no browser, run `getflightplan login --headless`. The
command shows a short code and an address. Open that address on another
device and enter the code.

`getflightplan logout` removes the stored credential from this machine. To
revoke it on the service, use the `/devices` page.

## Configuration

- `FLIGHTPLAN_URL` — `https://api.getflightplan.com`
- `FLIGHTPLAN_API_KEY` — your key (the MCP server's env; the stop hook also
  reads `~/.config/flightplan/env`).
- `.flightplan.toml` — the per-repo pin: a `repo` name and `url`, or a
  `target_id` with a readable `name` once the repo has an id pinned.

## What your agent is told

The installer adds the following managed contract with your repo name pinned.

<details>
<summary>View the agent instructions</summary>

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
- The registry is advisory and must never block work: if its tools are missing
  or error, proceed with the work, and tell your user once that they can run
  `uvx getflightplan install` (see getflightplan.com) to join this repo's
  registry.

</details>

## Data

What leaves your machine is the coordination record: intent summaries and
outcome paragraphs, glob patterns, changed-file paths, branch names, and
commit ids — sent only to the FlightPlan service. Source code
contents are never uploaded. Everything the registry knows, it learns as a
byproduct of your agents' work.

The details — what never leaves, and what is stored where — are in
[docs/data-flow.md](docs/data-flow.md). Vulnerability reporting:
[SECURITY.md](SECURITY.md).

## License

Apache-2.0
