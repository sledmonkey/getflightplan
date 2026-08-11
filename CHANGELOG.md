# Changelog

All notable changes to the FlightPlan client are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- A way to say that work has landed. An intent completed with
  `uncommitted: true` says the work is in one working tree and nowhere else,
  and the registry keeps warning everyone who touches those paths until it is
  told otherwise — it cannot see your tree. The new `mark_intent_landed` MCP
  tool and the new `getflightplan landed <intent-id>` command record the
  moment the work reached git. Landing can be repeated safely, and it never
  rewrites the completed record.
- `getflightplan landed` takes commit ids with a repeatable `--commit` flag.
  They are optional: the timestamp alone is the correction. The client never
  guesses which commits belong to an intent.

### Changed
- The service now refuses a completion retry that changes `outcome`,
  `summary`, `title`, `touches`, `branch`, or `uncommitted` on an intent that
  is already done or abandoned. The answer is 409. Send the same values again
  to retry, post a decision to correct an outcome, or use the landed verb for
  the git facts. Reported `files` and `commits` still accrete as before.

## [0.13.2] — 2026-08-10

### Changed
- One credential path (decision 72315903). The machine credential lives in
  `~/.config/flightplan/env`, written by `getflightplan login`. MCP
  registration reads only that file. It no longer reads the
  `FLIGHTPLAN_API_KEY` environment variable, which could register a stale
  token during rotation. The variable still works as plumbing for the MCP
  server process and the stop hook.
- Registration runs without prompts, for each agent binary on the machine.
  The API-key paste prompt is gone, and install lost `--no-input` — nothing
  prompts now. A terminal is not needed; piped and CI runs register too.
- `getflightplan login` completes the MCP registration after it stores the
  credential. A fresh machine needs two commands: install, then login.
  Login gained `--source`, matching install, so a development registration
  is not replaced with the PyPI package.
- `getflightplan install` prints one "next" line with the login command when
  the machine has no credential.
- A registration whose stored credential differs from the env file — or that
  has none — counts as stale. A new login therefore rotates the credential
  into the MCP registration on the next install or login run. The values are
  compared, never shown.
- When a replacement `mcp add` fails, the removed registration is put back
  from its stored entry, so a failed repair no longer deletes a working
  registration. Claude repairs and restores stay in the original project,
  local, or user scope.
- On a machine with no credential, install reports the missing registration
  and the stop hook as short pending lines ("..") instead of errors ("!!"),
  because the login fixes both. The loud lines with the manual command
  remain for the real problem: a credential exists and registration is still
  missing.
- Install repairs the registration before it prints the verify report, so
  the report shows the end state. A finding that install fixes by itself is
  never shown as an error first.
- When an agent binary is not on the machine, the verify line says so
  ("Claude Code is not on this machine — skipped") instead of promising
  that the login will register it.

## [0.13.0] — 2026-08-09

### Added
- `getflightplan login` now finds this repository in the registry. The command
  runs the check after it stores the credential. The check cannot fail the
  login. If the check has a problem, the command prints one line, and the
  login still succeeds. Use `--no-register` to skip the check.
- The check sends two git facts: the address of the `origin` remote, and up to
  1000 commit ids from `HEAD` backwards. The service normalizes the address,
  so two forms of the same address are one repository. The commit ids prove
  that you have a clone. The client sends no other git data.
- If your account has access to the repository, the client writes the id and
  the name to `.flightplan.toml`. The client keeps every other line in that
  file, and it keeps the comments. The write is atomic.
- If the registry does not know the repository, the client asks you to
  register it. On yes, the command opens a page in your browser and shows the
  address as well. Then it waits until you finish. If the code expires, the
  command tells you to try again.
- If the registry knows the repository, but your account has no access, the
  client asks you to request access. If the repository is invite only, the
  service answers with the reason, and the client prints it.
- A pending request gets a notice. Your work is recorded privately, under your
  personal account. Shared collision checks and shared context from the
  repository are not available. Approval changes your future posts only.
- `getflightplan register` runs the same check on its own. Use it after a
  login that you skipped, or to try again. The command needs a stored
  credential. Without one, it tells you to run `getflightplan login`.
- A shallow clone gets a warning. A shallow clone holds only part of the
  history, so the check can miss a registration that already exists.
- The check is skipped in three conditions. The pin file already holds an id.
  A project pin covers the folder. Or the folder has no git origin remote.
- The client cleans the names it gets from the service. An owner chooses the
  name of a repository, so the name is text from a different person. The
  client removes the control characters before it prints the name, so the
  name cannot change what you see in your terminal. The client also removes a
  double quote and a backslash before it writes the name to
  `.flightplan.toml`, so the name cannot add a key to that file. Every value
  in the pin file is escaped as well. The client removes every character in
  the Unicode category Cc. This includes the C1 controls, because U+009B can
  start an escape sequence on its own. The client also removes the bidi
  overrides, U+202A to U+202E and U+2066 to U+2069, because they change the
  order the text is drawn in. Usual text stays as it is. Accents, CJK, and
  emoji are correct in a name.

### Changed
- The client reads and writes `.flightplan.toml` as UTF-8. TOML is UTF-8 by
  specification. Before, the locale of the machine decided the encoding, so a
  name with an accent in it stopped the command on a machine with the C
  locale.

## [0.12.0] — 2026-08-09

### Added
- `getflightplan login` gets a credential from the service. You do not copy
  an API key.
- The command opens your browser at the approval page of the service. You
  approve there. The service then sends the browser to a listener on this
  machine. The listener uses the address 127.0.0.1 and a free port. It starts
  before the browser, and it closes after the one callback.
- The command uses PKCE. It keeps a secret verifier in the process. It sends
  only a hash of the verifier to the service. The one-time code from the
  callback is of no use without the verifier.
- If no approval arrives in 5 minutes, the command stops. It then tells you
  about the `--headless` flow.
- `getflightplan login --headless` logs you in with a code. The command shows
  a short code and an address. You open that address on another device and
  enter the code. The command waits until you approve.
- The client changes to the code flow without a question in two conditions:
  the listener cannot open a port, or the browser does not start.
- The credential goes to `~/.config/flightplan/env` with mode 600. This is the
  file that already holds the API key, so the MCP server and the stop hook
  find it. The client never prints the credential. The client never writes it
  to `.flightplan.toml` or to any other file in the repository.
- A login rotates the credential of this machine. The command sends the id of
  the stored credential, and the service revokes that credential when it
  mints the new one. The id is not a secret. Only your own credential can be
  replaced this way.
- `getflightplan logout` removes the credential from this machine. It keeps
  every other line in the file. To revoke the credential on the service, use
  the `/devices` page.

### Changed
- The write of `~/.config/flightplan/env` is now atomic. The new content goes
  to a temporary file with mode 600 first. Then it replaces the old file in
  one step. A line in the file that is not the key stays as it is.

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
