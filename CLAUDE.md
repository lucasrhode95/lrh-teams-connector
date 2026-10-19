# Repository agent instructions

`CLAUDE.md` (Claude Code) and `AGENTS.md` (Codex and other agents) must be identical. Change both together.

The first part, **Shared rules**, is the same in every `lrh-*` repository: change it in all of them at
once. The second part holds this repository's own rules.

# Shared rules

## Process and persistence

This is a local, single-user tool unless this repository documents otherwise. Keep processes scoped to
their documented entry point or session. Do not add schedulers, sync loops, watchers, warm-up tasks,
remote listeners, or multi-user features unless asked.

- Do not assume in-memory state survives across calls or processes. Return what a later call needs or
  read it from the server.
- Serve cached data only while it meets the documented freshness policy. If fresh data is required, wait
  for refresh instead of serving stale data.
- Shared local files (auth state, caches, indexes, output folders) must tolerate concurrent processes.

## Input boundaries and code structure

Validate untrusted input once at the entry point that receives it, and let internal routines trust the
validated contract. Document entry-point validation and internal assumptions in docstrings. When adding a
caller to a routine that assumes sanitized input, make sure that caller provides it.

Order modules top-down: public entry points first, then called functions, then private helpers and
formatting. Keep calls followable with named functions; prefer explicit branches over string-based
dispatch. Import the names used. Keep test-only seams in tests; constructor injection that is part of the
design stays. Modules that do the same job in several repositories (logging, local files, domain errors)
keep the same names and behavior; change them in every repository together.

## Checks, tests, and documentation

Run every check documented for this repository before each commit and fix failures. Never skip, disable,
or weaken a check to get a green result. Add or update tests for behavior changes and use synthetic
fixtures, never real captures or private user content. Update affected documentation in the same change,
and date new live research findings.

Documentation keeps each file to its own scope: requirements (what it must do, decisions, ownership) and
architecture (how it is built) are numbered; the research record holds only current findings that
Microsoft does not document or documents poorly; the roadmap holds open work only, split into
implementation, decisions and live verification. Keep documentation direct and summarized. Text files use
LF line endings (`.gitattributes`).

## Private data, external effects, and stored formats

Never log or commit credentials, tokens, session cookies, private user content, or real identifiers. Use
this repository's documented secure storage for authentication state. Logs carry operational timings,
statuses and counts only. Do not perform external writes without the user's explicit request. Retry a
write automatically, a bounded number of times and honoring `Retry-After`, only when the failure proves it
was not applied: the request was never sent, or the service answered 429 or 503. Never resend a write whose
outcome is uncertain (sent without a complete answer, or answered 504); report it so it can be checked.
When a multi-step write fails, undo the steps it already completed where that is safe, and report what
could not be undone. Resuming a partial upload from the ranges the service reports missing is allowed.

Treat persisted data as the current format. Do not add backward-compatibility shims, old-format readers,
automatic migrations, schema-version frameworks, or fallback behavior for previous application versions
unless asked. Keep safeguards that protect current user data and account ownership.

## Research scope

Research probes make explicitly authorized requests to configured endpoints and report responses. They do
not consume browser captures. HARs, Resource Saver archives and downloaded source bundles are temporary
inputs for ad hoc exploratory work outside the repository. Document useful URLs, request/response shapes,
dated findings and limitations. Do not retain raw or anonymized captures, per-file/member manifests, source
indexes, or capture-reading, regeneration and redaction machinery in the repository. Probes must work
without captures.

## Portability

Keep source, documentation, comments and fixtures free of real personal names, account addresses,
customer/project names and organization identifiers. Use fictional examples and synthetic content.
Deployment-specific URLs, clients, scopes and any expected-account checks belong in named configuration
settings. Microsoft public endpoints and first-party client IDs may be baseline settings; keep regional
service discovery and account-ownership checks.

The research record is an anonymized, dated starting point for forks and future policy changes. State
which client, resource, operation and evidence support a finding, without identifying the account or
organization. One environment's grants, denials or empty responses are not universal restrictions;
reassess capabilities before choosing a backend for a deployment. Flag genuinely organization-exclusive
endpoints or fixed-account dependencies for the user's decision before changing them. Standalone research
scripts require an explicit private config file rather than embedded fallback settings.

# Teams connector rules

## The project

A local, single-user Microsoft Teams connector with an MCP server and a user-operated local export UI. This repository is `lrh-teams-connector`, the active Teams project. Its canonical remote is the private GitHub repository; keep it separate from the Outlook connector until a merge is requested.

- User and operator guide: `README.md`
- Documentation index: `docs/README.md`
- What it must do: `docs/requirements.md`
- Architecture, module guide and MCP tool reference: `docs/architecture.md`
- API evidence and limitations: `docs/api-research.md`
- Open work: `docs/roadmap.md`

The Outlook, Teams, and SharePoint connectors use application-owned Microsoft sign-in through the shared `lrh-m365-auth` package and one encrypted cross-process MSAL cache by default. Microsoft handles the user's password and verification. One active account is shared across the connectors; account switching requires explicit shared sign-out and sign-in. MCP uses stdio; the local UI binds to loopback. Teams reads may use Microsoft Graph and documented-in-research Teams service routes. Treat reverse-engineered routes as private and undocumented, verify them feature by feature, and do not describe them as a stable public API.

## Checks

Run all five before every commit:

```bash
uv run ruff check .
uv run ruff format --check .   # use `uv run ruff format .` to fix
uv run pyright
uv run pytest
node --test tests/surfaces/export_ui.test.cjs
```

Use synthetic fixtures and fake transports only. Never use live chat content, identities, or captures in tests.

## Teams-specific safety and authentication

- Sending a Teams message and deleting the user's own chat messages are the only write paths. A send requires explicit authorization for both destination and content; a delete requires one explicit confirmation of the listed set of messages. Sends and deletes follow the write-retry rule above: never resend one whose outcome is uncertain.
- Keep logs limited to operational timings, statuses, and counts. Do not log message bodies, search terms, chat names, identifiers, tokens, or authorization headers. Keep MCP protocol stdout free of diagnostics.
- Normal UI, MCP, and sign-in use the shared encrypted cache provided by `lrh-m365-auth`. Do not fall back to plaintext if encrypted storage is unavailable.
- For an explicitly authorized local development probe only, pass `--unsecure` to a supported `teams-connector` command and set `M365_AUTH_HOME` to an isolated local path. This selects a separate plaintext cache containing reusable tokens. Never print it, copy it into logs, commit it, or share it; remove it when the probe is complete. Do not enable plaintext mode implicitly.
- Use the project's `.venv` for direct probes when available. If an authorized read-only probe fails with a network `ConnectionError` in the sandbox, retry only that same probe through the approved network-access path; do not change authentication routes.
- Do not access live Teams data unless the user requested or authorized that probe.

## Compatibility and documentation

Keep account-ownership safeguards and current-format validation. When behavior changes, update the affected README and architecture, MCP, requirements, roadmap, or research docs in the same change. Record dated live findings in the roadmap and research docs.
