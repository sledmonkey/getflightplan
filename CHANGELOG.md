# Changelog

All notable changes to the FlightPlan client are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [unreleased]

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
