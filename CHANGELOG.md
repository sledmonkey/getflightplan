# Changelog

All notable changes to the FlightPlan client are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.11.0] — 2026-08-09

### Added
- Under a project pin, a `list_intents` check with `overlaps` also asks each
  child repository that the globs touch. Before, the check asked the project
  only, and it did not find work posted inside a child repository. Each child
  repository is asked with its own paths. A glob that starts with `**` goes
  to every child repository unchanged. A glob with a wildcard first segment,
  like `agent*/src/**`, goes to each child directory that matches. A check of
  a child repository uses the globs only, because a collision across two
  scopes is deterministic. The judge still runs one time, on the query of the
  project. The results are merged into one list. For one intent, the project
  result wins over a child copy of that intent. Between two child results,
  the loudest alert wins. If a child repository cannot be reached, the other
  results still come back with a `note` that names it. The full check at the
  time you post an intent does not change.

## [0.10.0] — 2026-08-09

### Added
- The pin file `.flightplan.toml` accepts a third shape: `target = "project"`.
  A project pin binds a workspace folder. The child directories of the
  workspace are separate repositories. Each child has its own repository pin.
- Under a project pin, the client makes each path relative to the workspace
  root. Then the client maps each path to a child repository.
- If all paths are in one child repository, the client posts a repository
  intent. This intent is the same as an intent posted from inside that
  repository.
- If the paths are in two or more repositories, the client posts a project
  intent. This intent includes a `repositories` field. The field divides the
  paths by repository. The registry uses the field for collision checks
  across repositories.
- When you update or complete an intent, the client first asks the registry
  where the intent is stored. The client does not keep this state itself.
- The `list_intents` pre-planning check sees only project intents. The
  registry does the full cross-repository check when you post.
- The session-end stop hook also checks the child repositories of the
  project. An open intent in a child repository keeps the session open.
- The installer keeps a project pin as it is. It does not replace it, and it
  does not invent one.

### Changed
- Under a project pin, the client rejects a path that points out of the
  workspace. This is the same rule that rejects a path that points out of a
  repository.

## [0.9.2] — 2026-08-09

### Added
- `.flightplan.toml` accepts a second shape: `target`/`target_id` plus a
  readable `name`, alongside the existing `repo` name pin. Both are read
  everywhere (MCP client, installer, stop hook). When an id is pinned, posts
  carry it and go under the pinned name. Reads are untouched by routing: a
  `list_intents` query naming this repo gains the id, while one naming another
  repo — or naming none — goes exactly as asked, so cross-repo history queries
  keep working. The session-end stop hook sends the id alongside the name on
  its check, so a drifted name cannot point it at the wrong repo. The client
  never asks the service to turn a name into an id.
- `getflightplan install` preserves a pinned `target_id` (and its `target` and
  `name`) when it regenerates the managed pin file. It never invents one.

### Changed
- The agent snippet's registry-unavailable fallback now points the user at
  `uvx getflightplan install` instead of only mentioning the failure.
- `touches`, `files`, and `overlaps` are canonicalized to repository-relative
  POSIX paths before they are sent, so the same file names the same string from
  the repo root, a nested directory, or a linked worktree. Glob patterns keep
  their pattern part. Inside a repository, values resolving outside it are
  rejected as a tool error and never sent; outside a git repo nothing is
  rewritten or rejected.

## [0.9.0] — 2026-08-03

### Added
- First PyPI release. Install with `uvx getflightplan install`; the GitHub
  source install still works when you need to pin a commit.
- `getflightplan uninstall`: removes everything install wrote in a repo —
  managed snippet blocks, `.flightplan.toml`, the `/registry-digest` command,
  the stop hook and its settings wiring. `--dry-run` previews; `--purge-key`
  also deletes the machine-level saved key; MCP deregistration is offered
  interactively (default no — registrations are machine-wide).
- Trust docs: `SECURITY.md` (private reporting via security@getflightplan.com
  or GitHub advisories) and `docs/data-flow.md` (what leaves your machine,
  what never does, and what is stored where).
- `getflightplan install` now inspects existing MCP registrations instead of
  only checking the name. A registration that points at the old GitHub source,
  a local checkout, or the legacy `intent-registry` name is reported as stale
  and the installer offers to re-register it.
### Changed
- The install command is now `uvx getflightplan install`, straight from PyPI.
  Generated registrations run `uvx getflightplan mcp` (no `--from`).
- Snippet warn rule softened: when an overlap is the very work the agent was
  asked to act on, or the task is read-only, the agent mentions it and keeps
  going instead of pausing for confirmation.

## [0.1] — 2026-07-30

### Added
- Initial public release (package 0.9.0). `getflightplan install|mcp`: the MCP
  stdio client and a one-command install kit — writes the agent snippet into
  `CLAUDE.md`/`AGENTS.md` between managed markers, pins the repo name in
  `.flightplan.toml`, installs the `/registry-digest` command and the
  session-end stop hook, then verifies and offers to fix MCP registration for
  Claude Code and Codex (one key prompt, saved-key reuse, re-verify).
  Pre-PyPI install: `uvx --from git+https://github.com/sledmonkey/getflightplan
  getflightplan install`. Apache-2.0.
