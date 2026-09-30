# Teams Exporter

The Teams connector is the interface this application uses to communicate with Microsoft Teams APIs. It follows Teams Desktop client and authentication conventions while using endpoint behavior reverse-engineered from Teams Web traffic. We assume the relevant Desktop and Web clients use the same backend endpoints; that may need to be verified feature by feature. This work is also inspired by [Maxim-Mazurok/teams-api](https://github.com/Maxim-Mazurok/teams-api).

## Sign-in

Sign-in is handled by Microsoft/Teams. The user enters a password and completes any required verification on the identity provider's own sign-in experience; this application never sees or stores the user's password. After authentication, Microsoft provides opaque access tokens and manages refresh credentials as needed; the connector uses authorization tokens for API requests. The specific sign-in experience and application features may evolve.

The backend also exposes read-only operations through a standard MCP server over `stdio`, which MCP clients launch on demand. Search uses a persistent local SQLite archive; results include coverage information for the chats and dates actually indexed. See the [MCP server guide](docs/teams-mcp-server.md) for setup and the [product roadmap](docs/teams-exporter-roadmap.md) for scope and retention limits.

## Local export page

Start the local page from the repository root with `python -m teams_exporter serve` (add `--port 8765` to choose a port), then open `http://127.0.0.1:8765`. It lists conversations in activity order and lets you filter by name, choose a local start date and a per-chat message limit, then download one chat as text or multiple chats as a ZIP. Leave the count blank for all matching messages. The ZIP can contain one text file per chat or one combined text file. The server listens only on loopback; stop it with Ctrl+C.

If the page reports that sign-in is needed, run `python -m teams_exporter auth` from the repository root, finish Microsoft's device-code flow, then refresh the page. The local page and its APIs are read-only and do not mark conversations as read.

## More information

- [Teams API research](docs/teams-api-research.md)
- [Product roadmap](docs/teams-exporter-roadmap.md)
- [MCP server guide](docs/teams-mcp-server.md)
