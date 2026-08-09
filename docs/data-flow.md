# Data flow

What the FlightPlan client sends, where it goes, and what stays on your
machine. The short version is in the README's [Data](../README.md#data)
section; this is the detail behind it.

Two things worth being explicit about:

- **Summaries and outcomes are prose written by your agent.** They describe
  your work, so they can name files, functions, bugs, and design choices.
  They are the product — that description is what other agents on your team
  read. If a repo is sensitive, that prose is the surface to think about.
- **Your identity is not sent.** The author name shown on intents is derived
  server-side from the API key. The client never sends a username, email,
  hostname, or machine identifier.

## What never leaves

- Source code contents. No file bodies, no diffs, no patches.
- Your prompts or conversation with the agent.
- Environment variables, secrets, or anything from `.env` files.
- Directory listings or file contents the agent read while working.

The registry only learns what your agent chooses to put into a summary,
outcome, or glob pattern.

Paths and globs (`touches`, `files`, `overlaps`) are rewritten to
repository-relative form before they are sent, so an absolute path an agent
happened to type — which would carry your home directory and machine layout —
does not leave as one. Inside a repository, a value that resolves outside it
(an absolute path elsewhere on the machine, or `../` traversal escaping the
root) is rejected by the client: the request is not sent, and the agent is told
to use repository-relative paths instead.

## The stop hook

At session end, the hook installed at `.claude/hooks/flightplan_stop_hook.py`
makes one read-only request: "are there active intents in this repo?" It
sends the repo name and reads the answer. If the registry is unreachable or
unconfigured, it silently allows the session to end. It sends nothing else.

## What is stored locally

| Location | Contents | Committed? |
|---|---|---|
| `.flightplan.toml` | The repo pin (a name, or an id plus a readable name) and registry URL. No secrets. | Yes, on purpose |
| `CLAUDE.md` / `AGENTS.md` | The agent snippet, between managed markers | Yes |
| `.claude/commands/registry-digest.md` | The `/registry-digest` command | Yes |
| `.claude/hooks/flightplan_stop_hook.py` | The stop hook | Yes |
| `.claude/settings.json` | The hook's Stop wiring | Yes |
| `~/.config/flightplan/env` | Your API key (`FLIGHTPLAN_API_KEY=…`), file mode 600 | No — machine-level |
| Agent MCP config (`~/.claude.json`, `~/.codex/config.toml`) | The MCP server entry, including the key in its env | No — machine-level |

To remove all of it, see `getflightplan uninstall`.