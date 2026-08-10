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

When `.flightplan.toml` pins a workspace rather than a single repository, the
same rule measures from the workspace directory instead, so paths are sent
relative to it and carry the child repository's directory name. Values
resolving outside the workspace are rejected the same way.

## Finding and registering a repository

`getflightplan login` and `getflightplan register` ask the service which
repository this checkout is. Four calls exist, and each one carries your
credential in an `Authorization: Bearer` header.

| Call | What goes out | What comes back |
|---|---|---|
| `POST /repos/lookup` | The address of the `origin` remote, raw, and up to 1000 commit ids from `HEAD` backwards | The repository id, its readable name, its enrollment policy, and your access — or no match |
| `POST /repos/register/start` | The same remote address, the commit id of `HEAD`, and a readable name derived from the remote | A code, a page to open, how long the code lives, and how often to poll |
| `GET /repos/register/poll` | The code only | Pending, complete (with the repository id and name), or expired |
| `POST /repos/{id}/requests` | Nothing but the repository id in the path | Pending, or granted. A 403 says the repository is invite only |

Two things about the git facts. The remote address goes out as git holds it,
because the service normalizes it — that is how `git@host:a/b` and
`https://host/a/b` become one repository. The commit ids are ids only, never
messages, file names, diffs, or author data; they exist so that knowing the
address of a private repository is not enough to join it.

Nothing else about your history is sent, and no branch names go out.

## The stop hook

At session end, the hook installed at `.claude/hooks/flightplan_stop_hook.py`
makes one read-only request: "are there active intents in this repo?" It
sends the repo name and reads the answer. If the registry is unreachable or
unconfigured, it silently allows the session to end. It sends nothing else.

## What is stored locally

| Location | Contents | Committed? |
|---|---|---|
| `.flightplan.toml` | The pin (a name, or an id plus a readable name — for a repository or a workspace) and registry URL. No secrets. | Yes, on purpose |
| `CLAUDE.md` / `AGENTS.md` | The agent snippet, between managed markers | Yes |
| `.claude/commands/registry-digest.md` | The `/registry-digest` command | Yes |
| `.claude/hooks/flightplan_stop_hook.py` | The stop hook | Yes |
| `.claude/settings.json` | The hook's Stop wiring | Yes |
| `~/.config/flightplan/env` | Your API key (`FLIGHTPLAN_API_KEY=…`), file mode 600 | No — machine-level |
| Agent MCP config (`~/.claude.json`, `~/.codex/config.toml`) | The MCP server entry, including the key in its env | No — machine-level |

To remove all of it, see `getflightplan uninstall`.