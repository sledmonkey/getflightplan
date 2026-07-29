---
description: Narrate recent team work from the FlightPlan outcome feed
argument-hint: [window] [topic words | area/globs/* | author:name]
---

Produce an on-demand digest from the team intent registry (MCP server:
`flightplan`). The output is ephemeral chat text — don't write it to a
file or post it anywhere. (The registry's own `/digest` and `/receipts` pages
are the standing narrative surfaces; this command is the on-demand, scoped
variant.)

## Scope (from $ARGUMENTS; all optional)

Default: this repo, last 14 days, everything.
- A duration token (`7d`, `2w`, `48h`) → the time window (`since`).
- `author:<name>` → only that person's intents (`author`).
- Tokens containing `/` or `*` → area globs (pass as `overlaps`).
- Anything else → topic words (pass as `q`).

## Retrieval

Use `list_intents` with repo = the name pinned in `.flightplan.toml` at
the repo root (fallback: basename of the git origin remote, else the repo root
directory name). Fetch `status="done,abandoned"` for the narrative, and
`status="active"` for the closing "in flight" section. For topic queries that
come back thin, loosen before concluding nothing happened: retry with
`match="any"`, then with fewer words. Read the full outcomes — they are the
source, not the summaries alone.

## Synthesis rules

- Group by theme, not chronology. Lead each theme with what changed and *why*.
- Preserve the surprises, dead ends, and things deliberately left alone —
  that context is the payload; do not flatten the digest into a changelog.
- Where an outcome records that a `warn` overlap changed the work
  (coordinated / narrowed / proceeded anyway), say so.
- Cite intent ids (first 8 chars) so a reader can pull any full record.
- Close with: what's in flight right now, plus open threads — abandoned work
  someone might resume, and anything declared `uncommitted` that may not have
  landed.
- Length tracks activity: a quiet window gets a short digest that says so
  plainly. Never pad.
