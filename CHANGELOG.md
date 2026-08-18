# Changelog

All notable changes to the FlightPlan client are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.13.4] - 2026-08-18

### Added
- `glama.json` at the repo root claims the FlightPlan listing in the Glama
  MCP directory.

### Changed
- The README now says the hosted service is in beta, not private beta.
  Anyone can sign in with a GitHub account; there is no access request.
- `docs/data-flow.md` now documents the one identity exception. Login sends
  your machine hostname once, as the label for the new credential. No intent
  call sends it, and nothing else about the machine goes out.

## [0.13.3] - 2026-08-11

### Added
- An intent completed with `uncommitted: true` warns everyone who touches
  those paths, because the registry cannot see your tree. The new
  `mark_intent_landed` tool and `getflightplan landed <intent-id>` command
  record that the work reached git.
- Repeat `landed` calls are safe. They never rewrite the completed record.
- `getflightplan landed` takes commit ids with a repeatable `--commit`. They
  are optional: the timestamp alone is the correction. The client never
  guesses which commits belong to an intent.

### Changed
- The service now refuses completion retries that change `outcome`, `summary`,
  `title`, `touches`, `branch`, or `uncommitted` on an intent that is done or
  abandoned. Those get a 409 answer. Resend the same values to retry, or post
  a decision to correct an outcome.
- Reported `files` and `commits` still merge into a completed intent as
  before. Use `getflightplan landed` to add the git facts later.

## [0.13.2] - 2026-08-10

### Changed
- One credential path (decision 72315903). `getflightplan login` writes the
  machine credential to `~/.config/flightplan/env`, and MCP registration reads
  only that file. It no longer reads `FLIGHTPLAN_API_KEY`, which could
  register a stale token during a rotation.
- The MCP server process and the stop hook still read `FLIGHTPLAN_API_KEY`.
- Registration runs without prompts, for each agent binary on the machine.
  `getflightplan install` lost `--no-input`, because nothing prompts now. The
  command needs no terminal, so piped runs and CI runs register too.
- `getflightplan login` finishes MCP registration after it stores the
  credential. A fresh machine runs `getflightplan install`, then
  `getflightplan login`. Login gained `--source`, which keeps a development
  registration in place.
- `getflightplan install` prints one "next" line with the login command when
  the machine has no credential.
- A registration is stale when its stored credential differs from the env
  file, or is missing. So a new login rotates the credential in on the next
  install or login run. The client compares the values but never shows them.
- A failed replacement `mcp add` now puts the removed registration back, so a
  failed repair no longer deletes a working one. Repairs and restores stay in
  the original project, local, or user scope.
- On a machine with no credential, install shows pending lines ("..") for the
  missing registration and the stop hook, not errors, because login fixes
  both. The loud manual-command lines remain for the real problem: a
  credential that exists with a registration still missing.
- Install repairs the registration before it prints the verify report, so the
  report shows the end state. Install never reports a finding it fixed itself
  as an error.
- When an agent binary is not on the machine, the verify line says "Claude
  Code is not on this machine — skipped". Before, it promised that login would
  register the binary.

## [0.13.0] - 2026-08-09

### Added
- `getflightplan login` now finds this repository in the registry, after it
  stores the credential. The check can never fail the login; a problem prints
  one line. Use `--no-register` to skip it.
- The check sends two git facts: the `origin` remote address, and up to 1000
  commit ids from `HEAD` backwards. The service normalizes the address, so two
  forms of it are one repository. The commit ids prove you have a clone.
- With access to the repository, the client writes the id and the name to
  `.flightplan.toml`. It keeps every other line and every comment in that
  file.
- If the registry does not know the repository, the client offers to register
  it. Answering yes opens a page in your browser, prints the address, and
  waits. An expired code tells you to try again.
- If the registry knows the repository but your account has no access, the
  client offers to request access. An invite-only repository answers with the
  reason, which the client prints.
- A pending request gets a notice. Your work records privately under your
  personal account, without shared collision checks or shared context.
  Approval changes your future posts only.
- `getflightplan register` runs the same check on its own. Use it after a
  login you skipped, or to try again. It needs a stored credential, and
  without one it tells you to run `getflightplan login`.
- A shallow clone gets a warning. It holds only part of the history, so the
  check can miss a registration that already exists.
- The check is skipped when the pin file already holds an id, a project pin
  covers the folder, or the folder has no git `origin` remote.

### Changed
- The client reads and writes `.flightplan.toml` as UTF-8, as the TOML
  specification requires. Before, the machine locale decided the encoding.
  Under the C locale, an accented name stopped the command.

### Security
- The client strips control characters and bidi overrides from
  service-supplied names before it prints or writes them. An owner picks the
  name of a repository, so it is text from a different person. A hostile name
  cannot alter your terminal or your pin file.
- Accents, CJK, and emoji pass through a name unchanged. Every value written
  to `.flightplan.toml` is escaped.

## [0.12.0] - 2026-08-09

### Added
- `getflightplan login` gets a credential from the service. You no longer copy
  an API key.
- The command opens your browser at the service approval page. The service
  then sends the browser to a listener on this machine, at 127.0.0.1 on a free
  port. The listener starts before the browser and closes after one callback.
- Login uses PKCE. The secret verifier stays in the process, and only its hash
  goes to the service. The one-time callback code is of no use without the
  verifier.
- If no approval arrives in 5 minutes, the command stops and points you at the
  `--headless` flow.
- `getflightplan login --headless` shows a short code and an address. You open
  that address on another device and enter the code. The command waits until
  you approve.
- The client switches to the code flow without asking in two conditions. The
  listener cannot open a port, or the browser does not start.
- The client writes the credential to `~/.config/flightplan/env` with mode
  600, where the MCP server and the stop hook find it. It never prints the
  credential. It never writes it to `.flightplan.toml` or any other file in
  the repository.
- A login rotates this machine's credential. The command sends the stored
  credential id, which the service revokes as it mints the new one. The id is
  not a secret, and you can replace only your own credential.
- `getflightplan logout` removes the credential from this machine and keeps
  every other line in the file. To revoke it on the service, use the
  `/devices` page.

### Changed
- The client now writes `~/.config/flightplan/env` atomically, so an
  interrupted write cannot damage the file. A line that is not the key stays
  as it is.

## [0.11.0] - 2026-08-09

### Added
- Under a project pin, a `list_intents` check with `overlaps` now also asks
  each child repository the globs touch. Before, it asked the project only, so
  it missed work posted inside a child repository.
- Each child gets the check with its own paths. A glob that starts with `**`
  goes to every child unchanged. A glob with a wildcard first segment, like
  `agent*/src/**`, goes to each matching child directory.
- A child check uses the globs only, because a collision across two scopes is
  deterministic. The judge still runs one time, on the project query.
- The client merges the results into one list. For one intent, the project
  result wins over a child copy. Between two child results, the loudest alert
  wins.
- An unreachable child repository does not lose the other results. The answer
  then holds a `note` that names that child. The full check when you post an
  intent does not change.

## [0.10.0] - 2026-08-09

### Added
- `.flightplan.toml` accepts a third shape: `target = "project"`. A project
  pin binds a workspace folder whose child directories are separate
  repositories. Each child keeps its own repository pin.
- Under a project pin, the client makes each path relative to the workspace
  root, then maps each path to a child repository.
- Paths that all sit in one child post a repository intent. It is the same as
  an intent posted from inside that repository.
- Paths across two or more repositories post a project intent with a
  `repositories` field. The field divides the paths by repository, and the
  registry uses it for collision checks across repositories.
- When you update or complete an intent, the client first asks the registry
  where the intent is stored. The client keeps no such state itself.
- The `list_intents` pre-planning check sees project intents only. The
  registry does the full cross-repository check when you post.
- The session-end stop hook also checks the project's child repositories. An
  open intent in a child repository keeps the session open.
- The installer keeps a project pin as it is. It does not replace it, and it
  does not invent one.

### Changed
- Under a project pin, the client rejects a path that points out of the
  workspace. This is the same rule that rejects a path that points out of a
  repository.

## [0.9.2] - 2026-08-09

### Added
- `.flightplan.toml` accepts a second shape: `target`/`target_id` plus a
  readable `name`. The existing `repo` name pin still works. The MCP client,
  the installer, and the stop hook read both shapes.
- When the file pins an id, a post carries the id and uses the pinned name.
  Routing does not change reads. The client never asks the service to turn a
  name into an id.
- A `list_intents` query that names this repository gains the id. A query that
  names another repository, or no repository, goes exactly as you asked. So
  history queries across repositories keep working.
- The session-end stop hook sends the id with the name on its check. A name
  that drifted cannot point the hook at the wrong repository.
- `getflightplan install` keeps a pinned `target_id`, `target`, and `name`
  when it regenerates the managed pin file. It never invents an id.

### Changed
- The fallback text in the agent snippet now points you at
  `uvx getflightplan install`. Before, the text only mentioned the failure.
- The client changes `touches`, `files`, and `overlaps` to POSIX paths
  relative to the repository before it sends them. So the same file gives the
  same string from the repository root, a nested directory, or a linked
  worktree. A glob pattern keeps its pattern part.
- Inside a repository, the client refuses a value that resolves outside the
  repository. It reports a tool error and sends nothing. Outside a git
  repository, the client changes nothing and refuses nothing.

## [0.9.0] - 2026-08-03

### Added
- First PyPI release. Install with `uvx getflightplan install`. The GitHub
  source install still works, for when you need to pin a commit.
- `getflightplan uninstall` removes everything install wrote in a repository.
  That covers the managed snippet blocks, `.flightplan.toml`, the
  `/registry-digest` command, and the stop hook with its settings. `--dry-run`
  shows a preview, and `--purge-key` also deletes the saved key.
- `getflightplan uninstall` asks you before it removes an MCP registration.
  The default answer is no, because a registration applies to the whole
  machine.
- Two trust documents. `SECURITY.md` tells you how to report a problem
  privately, at security@getflightplan.com or through a GitHub advisory.
  `docs/data-flow.md` tells you what leaves your machine, what never leaves,
  and where the service stores each item.
- `getflightplan install` now examines each existing MCP registration, not
  just its name. It reports one as stale when it points at the old GitHub
  source, points at a local checkout, or uses the old `intent-registry` name.
  Install then offers to register it again.

### Changed
- The install command is now `uvx getflightplan install`, straight from PyPI.
  A generated registration runs `uvx getflightplan mcp`, without `--from`.
- The snippet has a softer rule for a `warn` overlap. The agent mentions the
  overlap and keeps going when it is the very work you asked for, or the task
  is read-only. Before, the agent paused for confirmation.

## [0.1] - 2026-07-30

### Added
- Initial public release (package 0.9.0). It gives two commands:
  `getflightplan mcp`, the MCP stdio client, and `getflightplan install`, a
  one-command install kit. Apache-2.0.
- Install writes the agent snippet into `CLAUDE.md` and `AGENTS.md`, between
  managed markers. It pins the repository name in `.flightplan.toml`, and
  installs the `/registry-digest` command and the session-end stop hook.
- Install then verifies the MCP registration for Claude Code and Codex, and
  offers to fix one that is wrong. It asks for the key one time, uses the
  saved key after that, and verifies again.
- Before PyPI, install with
  `uvx --from git+https://github.com/sledmonkey/getflightplan getflightplan install`.

[Unreleased]: https://github.com/sledmonkey/getflightplan/compare/v0.13.4...HEAD
[0.13.4]: https://github.com/sledmonkey/getflightplan/compare/v0.13.3...v0.13.4
[0.13.3]: https://github.com/sledmonkey/getflightplan/compare/v0.13.2...v0.13.3
[0.13.2]: https://github.com/sledmonkey/getflightplan/compare/v0.13.0...v0.13.2
[0.13.0]: https://github.com/sledmonkey/getflightplan/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/sledmonkey/getflightplan/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/sledmonkey/getflightplan/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/sledmonkey/getflightplan/compare/v0.9.2...v0.10.0
[0.9.2]: https://github.com/sledmonkey/getflightplan/compare/v0.9.0...v0.9.2
[0.9.0]: https://github.com/sledmonkey/getflightplan/compare/v0.1...v0.9.0
[0.1]: https://github.com/sledmonkey/getflightplan/releases/tag/v0.1
