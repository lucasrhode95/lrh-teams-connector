# Repository agent instructions

## Compatibility policy

- Treat this repository as an actively developed application with a fresh current format.
- Do not add backward-compatibility shims, old-format readers, automatic data migrations, schema-version frameworks, or fallback behavior for previous application versions unless the user explicitly asks for it.
- When changing a current format, update its producers, consumers, fixtures, and tests together. Do not preserve obsolete formats “just in case.”
- Keep account-ownership safeguards and current-format validation; these protect present user data and are not format-compatibility layers.

## Development authentication

- Encrypted MSAL storage is the default for normal UI, MCP, and sign-in use.
- For a user-requested direct live connector/API probe, use `TeamsConnector(unsecure=True)` so the agent process reads the separate development cache. Constructing the connector does not activate the cached session; call `connector.login(interactive=False)` before making requests. For example:

  ```python
  from teams_exporter.teams_connector import TeamsConnector

  connector = TeamsConnector(unsecure=True)
  connector.login(interactive=False)
  chats = connector.list_chats()
  ```

  This was verified to load the development cache and fetch the chat catalog without prompting for sign-in. If that cache has not been signed in, tell the user to run `python -m teams_exporter auth --unsecure` and complete Microsoft's interactive sign-in.
- Use the project's `.venv` to run direct probes when available. If a live request fails with a network `ConnectionError` in the sandbox, retry only the same read-only probe through the approved network-access path; do not switch to another authentication route.
- Print only the small sanitized fields needed to inspect behavior. Never print or log tokens, cache contents, request authorization headers, or private message bodies unless the user explicitly requested those contents.
- Do not enable plaintext mode implicitly. Its cache is `.local/teams-msal-debug-cache.json` and contains reusable tokens. Never print, copy into logs, commit, or share that file; keep `.local/` ignored and delete the cache when development is finished.
- Do not use live Teams access unless the user requested or authorized that probe. Keep MCP protocol stdout free of diagnostics.
