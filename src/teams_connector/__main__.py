"""Command-line entry point: ``teams-connector <command>``.

Commands import their dependencies lazily, so that ``mcp`` never loads the web stack.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import UTC, datetime

from m365_auth import AccessToken, AuthenticationRequired, CacheStatus, TokenProvider

from teams_connector import config
from teams_connector.domain.errors import ConnectorError
from teams_connector.surfaces.ui_settings import DEFAULT_UI_IDLE_MINUTES, DEFAULT_UI_PORT

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH_REQUIRED = 2
EXIT_INTERRUPTED = 130

_SCOPE_FAMILIES = (
    "Channel",
    "ChannelMessage",
    "ChatMessage",
    "Chat.",
    "Files",
    "Sites",
    "User",
    "Team",
    "People",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=config.CLI_NAME, description="Local Microsoft Teams connector.")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="Sign in with a device code (the only command that ever prompts).")
    auth.add_argument("--sign-out", action="store_true", help="Sign out of the shared Microsoft account.")
    _add_unsecure(auth)

    status = sub.add_parser(
        "status", help="Show cached client credentials and resource-token state (offline by default)."
    )
    status.add_argument(
        "--check", action="store_true", help="Also acquire each profile's token (contacts Microsoft)."
    )
    status.add_argument("--json", action="store_true", help="Machine-readable output.")
    _add_unsecure(status)

    mcp = sub.add_parser("mcp", help="Run the MCP server over stdio (started by your MCP client).")
    _add_unsecure(mcp)

    ui = sub.add_parser("ui", help="Start the local export UI (stops when idle).")
    ui.add_argument(
        "--port",
        type=int,
        default=DEFAULT_UI_PORT,
        help=f"Preferred port (default {DEFAULT_UI_PORT}; a free one if busy).",
    )
    ui.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    ui.add_argument(
        "--idle-minutes",
        type=float,
        default=DEFAULT_UI_IDLE_MINUTES,
        help=f"Stop after this many idle minutes (default {DEFAULT_UI_IDLE_MINUTES}).",
    )
    _add_unsecure(ui)

    return parser


def _add_unsecure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--unsecure",
        action="store_true",
        help="Development only: use a separate PLAINTEXT shared-auth cache.",
    )


def main(argv: list[str] | None = None, *, provider_factory: type[TokenProvider] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.unsecure:
        print(
            "WARNING: --unsecure stores reusable Microsoft tokens in a separate PLAINTEXT shared-auth cache. "
            "Do not share it; delete it when done.",
            file=sys.stderr,
        )
    if args.command == "mcp":
        from teams_connector.logs import configure_app_logging
        from teams_connector.surfaces import mcp_main

        configure_app_logging("mcp")  # file only: stdout belongs to the MCP protocol from here on
        mcp_main.run(unsecure=args.unsecure)
        return EXIT_OK
    if args.command == "ui":
        from teams_connector.logs import configure_app_logging
        from teams_connector.surfaces.web import main as web_main

        configure_app_logging("ui", stdout=True)
        with contextlib.suppress(KeyboardInterrupt):
            web_main.run(
                unsecure=args.unsecure,
                port=args.port,
                open_browser=not args.no_browser,
                idle_minutes=args.idle_minutes,
            )
        return EXIT_OK
    try:
        if provider_factory is None:
            provider_factory = TokenProvider
        provider = provider_factory(unsecure=args.unsecure, login_command=f"{config.CLI_NAME} auth")
        if args.command == "auth":
            return _auth(provider, args)
        return _status(provider, args)
    except AuthenticationRequired as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_AUTH_REQUIRED
    except ConnectorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


def _auth(provider: TokenProvider, args: argparse.Namespace) -> int:
    if args.sign_out:
        existed = provider.sign_out()
        print("Signed out: token cache deleted." if existed else "No token cache to delete.")
        return EXIT_OK
    results = provider.bootstrap(show=lambda message: print(f"\n{message}\n", flush=True))
    for result in results:
        purpose = provider.profiles[result.profile].purpose
        if result.ready and result.token is not None:
            print(f"[{result.profile}] Ready: {purpose}.")
            _print_token_summary(result.profile, result.token)
        else:
            print(f"[{result.profile}] Not ready: {purpose}. {result.error}", file=sys.stderr)
    if all(result.ready for result in results):
        return EXIT_OK
    first_error = next((result.error for result in results if result.error is not None), None)
    return EXIT_AUTH_REQUIRED if isinstance(first_error, AuthenticationRequired) else EXIT_ERROR


def _status(provider: TokenProvider, args: argparse.Namespace) -> int:
    status = provider.status()
    checks: dict[str, dict[str, object]] = {}
    if args.check:
        for name in provider.profile_names:
            try:
                checks[name] = {"ok": True, **_token_facts(provider.get_token(name))}
            except ConnectorError as exc:
                checks[name] = {"ok": False, "error": str(exc)}
    if args.json:
        print(json.dumps(_status_dict(status, checks), indent=2))
    else:
        _print_status(status, checks)
    return EXIT_OK if all(check["ok"] for check in checks.values()) else EXIT_AUTH_REQUIRED


# ---------------------------------------------------------------------- formatting


def _token_facts(token: AccessToken) -> dict[str, object]:
    claims = token.claims()
    scopes = str(claims.get("scp", "")).split()
    return {
        "source": token.source,
        "audience": claims.get("aud"),
        "account": claims.get("upn") or claims.get("unique_name"),
        "scope_count": len(scopes),
        "teams_scopes": sorted(s for s in scopes if s.startswith(_SCOPE_FAMILIES)),
        "expires_on": _iso(token.expires_on),
    }


def _print_token_summary(name: str, token: AccessToken) -> None:
    facts = _token_facts(token)
    scopes = facts["teams_scopes"] or []
    print(f"  [{name}] {facts['audience']}  ({facts['scope_count']} scopes)  via {facts['source']}")
    if scopes:
        print(f"    {' '.join(str(s) for s in scopes)}")  # type: ignore[union-attr]


def _status_dict(status: CacheStatus, checks: dict[str, dict[str, object]]) -> dict[str, object]:
    return {
        "cache": {"mode": status.mode, "path": str(status.path), "exists": status.exists},
        "accounts": [{"username": a.username, "fingerprint": a.fingerprint} for a in status.accounts],
        "data_dir": str(config.data_dir()),
        "profiles": [
            {
                "profile": p.profile,
                "purpose": p.purpose,
                "client_id": p.client_id,
                "scopes": list(p.scopes),
                "client_credential_cached": p.client_credential_cached,
                "access_token_expires_on": _iso(p.access_token_expires_on),
                **({"check": checks[p.profile]} if p.profile in checks else {}),
            }
            for p in status.profiles
        ],
    }


def _print_status(status: CacheStatus, checks: dict[str, dict[str, object]]) -> None:
    print(f"Token cache: {status.mode}, {status.path}" + ("" if status.exists else " (not created yet)"))
    print(f"Data:        {config.data_dir()}")
    if not status.accounts:
        print("Account:     not signed in")
    for account in status.accounts:
        print(f"Account:     {account.username} (fingerprint {account.fingerprint})")
    for p in status.profiles:
        state = "cached" if p.client_credential_cached else "not cached"
        print(f"\n[{p.profile}] {p.purpose}")
        print(f"  client {p.client_id} -> {' '.join(p.scopes)}")
        print(
            f"  client credential {state}; access token valid until {_iso(p.access_token_expires_on) or '-'}"
        )
        if p.profile in checks:
            check = checks[p.profile]
            if check["ok"]:
                print(f"  check: ok ({check['source']}, {check['scope_count']} scopes)")
            else:
                print(f"  check: FAILED - {check['error']}")


def _iso(epoch: int | None) -> str | None:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds") if epoch else None


if __name__ == "__main__":
    raise SystemExit(main())
