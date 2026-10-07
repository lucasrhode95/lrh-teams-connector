# Repository agent instructions

`CLAUDE.md` (Claude Code) and `AGENTS.md` (Codex and other agents) must be identical. Change both together.

## Shared development rules

### Process and persistence

This is a local, single-user connector unless this repository documents otherwise. Keep processes scoped to their documented entry point or session. Do not add schedulers, sync loops, watchers, warm-up tasks, remote listeners, or multi-user features unless asked.

- Do not assume in-memory state survives across calls or processes. Return what a later call needs or read it from the server.
- Serve cached data only while it meets the documented freshness policy. If fresh data is required, wait for refresh instead of serving stale data.
- Shared local files (auth state, caches, indexes, output folders) must tolerate concurrent processes.

### Input boundaries and code structure

Validate untrusted input once at the entry point that receives it, and let internal routines trust the validated contract. Document entry-point validation and internal assumptions in docstrings. When adding a caller to a routine that assumes sanitized input, make sure that caller provides it.

Order modules top-down: public entry points first, then called functions, then private helpers and formatting. Keep calls followable with named functions; prefer explicit branches over string-based dispatch. Import the names used. Keep test-only seams in tests; constructor injection that is part of the design stays.

### Checks, tests, and documentation

Run every check documented for this repository before each commit and fix failures. Never skip, disable, or weaken a check to get a green result. Add or update tests for behavior changes and use synthetic fixtures, never real captures or private user content. Update affected documentation in the same change, and date new live research findings.

### Private data, external effects, and stored formats

Never log or commit credentials, tokens, session cookies, private user content, or real identifiers. Use this repository's documented secure storage for authentication state. Do not perform external writes without the user's explicit request. Make each external write once; do not retry automatically, and report uncertain outcomes.

Treat persisted data as the current format. Do not add backward-compatibility shims, old-format readers, automatic migrations, schema-version frameworks, or fallback behavior for previous application versions unless asked. Keep safeguards that protect current user data and account ownership.

## The project

A local, single-user Microsoft Teams connector with an MCP server and a user-operated local export UI. This repository is the active Teams project; it is expected to migrate into `lrh-teams-connector` later. Keep work here until that migration is requested.

- User and operator guide: `README.md`
- Architecture and module guide: `docs/project-architecture.md`
- API evidence and limitations: `docs/teams-api-research.md`
- MCP behavior: `docs/teams-mcp-server.md`
- Work register: `docs/teams-exporter-roadmap.md`

The connector uses application-owned Microsoft sign-in and an encrypted MSAL token cache by default. Microsoft handles the user's password and verification. MCP uses stdio; the local UI binds to loopback. Teams reads may use Microsoft Graph and documented-in-research Teams service routes. Treat reverse-engineered routes as private and undocumented, verify them feature by feature, and do not describe them as a stable public API.

## Checks

Run all four before every commit:

```bash
uv run ruff check .
uv run ruff format --check .   # use `uv run ruff format .` to fix
uv run pyright
uv run pytest
```

Use synthetic fixtures and fake transports only. Never use live chat content, identities, or captures in tests.

## Teams-specific safety and authentication

- Sending a Teams message is the only write path. Require explicit authorization for both destination and content. Send once; do not retry automatically after an uncertain result.
- Keep logs limited to operational timings, statuses, and counts. Do not log message bodies, search terms, chat names, identifiers, tokens, or authorization headers. Keep MCP protocol stdout free of diagnostics.
- Normal UI, MCP, and sign-in use encrypted MSAL storage. Do not fall back to plaintext if encrypted storage is unavailable.
- A user-requested local development probe may explicitly use `TeamsConnector(unsecure=True)` and `connector.login(interactive=False)` to select the separate development cache. Its `.local/teams-msal-debug-cache.json` file contains reusable tokens. Never print, copy into logs, commit, or share it; delete it when development is finished. Do not enable plaintext mode implicitly.
- Use the project's `.venv` for direct probes when available. If an authorized read-only probe fails with a network `ConnectionError` in the sandbox, retry only that same probe through the approved network-access path; do not change authentication routes.
- Do not access live Teams data unless the user requested or authorized that probe.

## Compatibility and documentation

Keep account-ownership safeguards and current-format validation. When behavior changes, update the affected README and architecture, MCP, requirements, roadmap, or research docs in the same change. Record dated live findings in the roadmap and research docs.