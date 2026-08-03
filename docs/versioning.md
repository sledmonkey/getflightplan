# Versioning

## Where the version lives

The `version` in `pyproject.toml` is the only version. Git tags follow it: the
tag for version `X.Y.Z` is `vX.Y.Z`. The release workflow fails if a tag and
the package version disagree.

To read the installed version at runtime, use
`importlib.metadata.version("getflightplan")`.

## Pre-1.0 semantics

We are below 1.0, so the usual semver rules are relaxed one place:

- **Minor bumps (0.9 → 0.10) may break things.** Flags can change, defaults can
  change, files the installer writes can change shape. Read the changelog.
- **Patch bumps (0.9.0 → 0.9.1) never break things.** Bug fixes only.

At 1.0 this tightens to normal semver.

## Dependencies

- `mcp>=1.2,<2` — MCP SDK 2.0 removes `mcp.server.fastmcp`, which this client
  uses. The upper bound is deliberate, not caution.
- `httpx>=0.27,<0.28` — pinned to one minor. httpx still makes breaking changes
  across minors.

These are the whole dependency surface. We add to it reluctantly.

## Service compatibility

The wire contract between this client and the hosted service is pinned by the
service's own contract tests, which run against the released client.

The promise here is narrow: a given client minor works against the hosted
service as of that release. If the service changes the contract, it stays
compatible with client minors already published, or the client gets a minor
bump and a changelog entry.
