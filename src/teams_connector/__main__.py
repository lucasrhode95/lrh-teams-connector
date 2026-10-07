"""Command-line entry point: ``teams-connector <command>``.

Commands import their dependencies lazily, so that ``mcp`` never loads the web stack.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from teams_connector import config
from teams_connector.domain.errors import AuthenticationRequired, ConnectorError

if TYPE_CHECKING:
    from teams_connector.auth.tokens import AccessToken, CacheStatus, TokenProvider

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
    auth.add_argument("--force", action="store_true", help="Sign in again even if a silent token works.")
    auth.add_argument("--sign-out", action="store_true", help="Delete the token cache.")
    _add_unsecure(auth)

    status = sub.add_parser("status", help="Show the signed-in account and profiles (offline by default).")
    status.add_argument(
        "--check", action="store_true", help="Also acquire each profile's token (contacts Microsoft)."
    )
    status.add_argument("--json", action="store_true", help="Machine-readable output.")
    _add_unsecure(status)

    mcp = sub.add_parser("mcp", help="Run the MCP server over stdio (started by your MCP client).")
    _add_unsecure(mcp)

    ui = sub.add_parser("ui", help="Start the local export UI (stops when idle).")
    ui.add_argument(
        "--port", type=int, default=8765, help="Preferred port (default 8765; a free one if busy)."
    )
    ui.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    ui.add_argument("--idle-minutes", type=float, default=30, help="Stop after this many idle minutes.")
    _add_unsecure(ui)

    backup = sub.add_parser("backup", help="Make a consistent copy of the local archive.")
    backup.add_argument("destination", help="Destination file, preferably ending in .sqlite3.")
    backup.add_argument("--overwrite", action="store_true", help="Replace an existing destination file.")
    _add_unsecure(backup)
    return parser


def _add_unsecure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--unsecure", action="store_true", help="Development only: use a separate PLAINTEXT token cache."
    )


def main(argv: list[str] | None = None, *, provider_factory: type[TokenProvider] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.unsecure:
        print(
            "WARNING: --unsecure stores reusable Microsoft tokens as PLAINTEXT at "
            f"{config.token_cache_path(unsecure=True)}. Do not share it; delete it when done.",
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
            from teams_connector.auth.tokens import TokenProvider as provider_factory
        provider = provider_factory(unsecure=args.unsecure)
        if args.command == "auth":
            return _auth(provider, args)
        if args.command == "backup":
            return _backup(provider, args)
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
    first = config.SIGN_IN_PROFILE
    signed_in = False
    if not args.force:
        try:
            provider.get_token(first)
            signed_in = True
        except AuthenticationRequired:
            pass
    if not signed_in:
        print("Signing in to Microsoft Teams.")
        provider.sign_in(first, show=lambda message: print(f"\n{message}\n", flush=True))
    # One sign-in normally serves every profile (same client); prompt again only if a profile needs it.
    for name in config.PROFILES:
        try:
            token = provider.get_token(name)
        except AuthenticationRequired:
            print(f"'{name}' needs its own consent: {config.PROFILES[name].purpose}.")
            token = provider.sign_in(name, show=lambda message: print(f"\n{message}\n", flush=True))
        _print_token_summary(name, token)
    print("Signed in. MCP and the UI will use this sign-in silently.")
    return EXIT_OK


def _backup(provider: TokenProvider, args: argparse.Namespace) -> int:
    from teams_connector.store.db import Store, store_path

    account = provider.cached_account()
    if account is None:
        raise AuthenticationRequired("No Teams sign-in found.", command=provider.sign_in_command())
    path = store_path(account.fingerprint)
    if not path.exists():
        print("There is no local archive for this account yet.", file=sys.stderr)
        return EXIT_ERROR
    target = Store(path, account.fingerprint).backup_to(args.destination, overwrite=args.overwrite)
    print(f"Teams archive backup created: {target}")
    return EXIT_OK


def _status(provider: TokenProvider, args: argparse.Namespace) -> int:
    status = provider.status()
    checks: dict[str, dict[str, object]] = {}
    if args.check:
        for name in config.PROFILES:
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
                "signed_in": p.signed_in,
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
        state = "signed in" if p.signed_in else "not signed in"
        print(f"\n[{p.profile}] {p.purpose}")
        print(f"  client {p.client_id} -> {' '.join(p.scopes)}")
        print(f"  {state}; cached access token valid until {_iso(p.access_token_expires_on) or '-'}")
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
