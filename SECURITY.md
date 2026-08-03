# Security policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately — do not open a public issue.

- Email **security@getflightplan.com**, or
- Use [GitHub private vulnerability reporting](https://github.com/sledmonkey/getflightplan/security/advisories/new)
  on this repository.

Include what you found, how to reproduce it, and the impact you see. We aim to
acknowledge reports within 3 business days.

## Scope

This repository is the FlightPlan client: the CLI, the MCP adapter, and the
install kit. Reports about the hosted service (api.getflightplan.com) go
through the same channels — email is preferred for service issues.

## What the client handles

The client sends coordination records (intent summaries, glob patterns, file
paths) to the hosted service and stores your API key locally. Source code
contents never leave your machine. Details are in
[docs/data-flow.md](docs/data-flow.md).
