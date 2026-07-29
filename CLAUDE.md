<!-- flightplan:begin — managed by getflightplan install; edits inside this block are overwritten -->
## Intent registry

This repo participates in the team intent registry (MCP server: `flightplan`).

- **Before starting non-trivial work**, call `post_intent`. The test: will the
  work change behavior, defaults, or contracts another agent would encounter —
  or, for pure investigation, would the findings save the next agent an hour?
  Yes to either → post; Q&A and typo-level fixes, no. Send
  a one-paragraph summary (what + why), `kind` (`build`, or
  `explore`/`spike` for throwaway investigation), and `touches` globs for the
  areas you expect to change. Keep the returned id for later. For `repo`,
  use `getflightplan` (pinned in `.flightplan.toml`; do not derive it) — every agent on this repo must use the same name or
  collision checks silently miss each other. The response may include
  `context`: recent completed work relevant to your task — read those outcomes
  before starting; the surprises and dead ends in them are load-bearing.
- If the response includes overlaps at level `warn`, tell your user who is doing
  what and which globs collide, and ask how to proceed before continuing.
  `fyi`/`nudge` levels: mention briefly and keep going.
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
<!-- flightplan:end -->
