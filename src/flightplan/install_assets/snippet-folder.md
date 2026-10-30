## Intent registry

This folder shares a team intent registry (MCP server: `flightplan`). Agents
working here record what they are about to do and what they learned, so the
next person's agent can see it.

- **Before work that others may use or edit**, call `post_intent`. The test:
  will the work create or change a file in this folder that someone else may
  use or edit — or would what you learn save the next person time? Yes to
  either → post. Questions, reading, and throwaway drafts nobody else will
  see → no. Send a one-paragraph summary (what + why), `kind` (`build` for
  work meant to stay, `explore` for an investigation), and `touches`: the
  files or folders you expect to change, relative to this folder. Keep the
  returned id for later. For `repo`, {repo_clause} — every agent in this
  folder must use the same name or collision checks silently miss each other.{connector_clause}
  The response may include `context`: recent finished work relevant to your
  task — read those outcomes before starting; the surprises and dead ends in
  them are load-bearing.
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
  deliberately left in place (a typo kept on purpose, a value you could not
  check). If a `warn` overlap changed how the work went (coordinated, narrowed
  scope, proceeded anyway), say which. Send `files`: the paths you actually
  changed, relative to this folder. Send `files: []` only when this work
  changed no files; omit `files` when you do not know. This folder has no
  git, so do not send `commits` or `uncommitted`. Completing an intent ends
  the slice, not the session: follow-up work after a complete that changes a
  shared file gets a fresh post — "same session" doesn't exempt it.
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
  `uvx getflightplan install` (see getflightplan.com) to join this folder's
  registry.
