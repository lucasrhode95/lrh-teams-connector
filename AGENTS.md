# Repository agent instructions

## Compatibility policy

- Treat this repository as an actively developed application with a fresh current format.
- Do not add backward-compatibility shims, old-format readers, automatic data migrations, schema-version frameworks, or fallback behavior for previous application versions unless the user explicitly asks for it.
- When changing a current format, update its producers, consumers, fixtures, and tests together. Do not preserve obsolete formats “just in case.”
- Keep account-ownership safeguards and current-format validation; these protect present user data and are not format-compatibility layers.

## Development authentication

- Encrypted MSAL storage is the default for normal UI, MCP, and sign-in use.
- For a user-requested direct live connector/API probe, use `TeamsConnector(unsecure=True)` so the agent process reads the separate development cache. If that cache has not been signed in, tell the user to run `python -m teams_exporter auth --unsecure` and complete Microsoft's interactive sign-in.
- Do not enable plaintext mode implicitly. Its cache is `.local/teams-msal-debug-cache.json` and contains reusable tokens. Never print, copy into logs, commit, or share that file; keep `.local/` ignored and delete the cache when development is finished.
- Do not use live Teams access unless the user requested or authorized that probe. Keep MCP protocol stdout free of diagnostics.
