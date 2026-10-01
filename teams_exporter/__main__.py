from __future__ import annotations

import argparse
import sys

from teams_exporter.teams_connector import TeamsConnector
from teams_exporter.teams_service import TeamsService
from teams_exporter.token_provider import AuthenticationRequiredError, TokenProvider


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m teams_exporter")
    commands = parser.add_subparsers(dest="command", required=True)
    auth_parser = commands.add_parser(
        "auth", help="complete the interactive Microsoft device-code sign-in"
    )
    auth_parser.add_argument(
        "--unsecure",
        action="store_true",
        help="store tokens in a plaintext development cache for direct agent probes",
    )
    serve_parser = commands.add_parser("serve", help="serve the local conversation export UI")
    serve_parser.add_argument("--port", type=int, default=8765, help="loopback port (default: 8765)")
    serve_parser.add_argument(
        "--unsecure",
        action="store_true",
        help="use the plaintext development MSAL cache instead of encrypted storage",
    )
    backup_parser = commands.add_parser("backup", help="make a consistent backup of the local Teams archive")
    backup_parser.add_argument("destination", help="destination file, preferably ending in .sqlite3")
    backup_parser.add_argument(
        "--overwrite", action="store_true", help="replace an existing destination file"
    )
    args = parser.parse_args()
    if args.command == "auth":
        connector = TeamsConnector(unsecure=args.unsecure)
        if args.unsecure:
            print(
                TokenProvider.unsecure_warning(connector.token_provider.cache_path),
                file=sys.stderr,
            )
        try:
            connector.login(interactive=True)
        except Exception as exc:
            # Authentication library diagnostics can contain sensitive values;
            # keep the CLI output actionable without echoing exception text.
            print(
                f"Teams sign-in failed ({exc.__class__.__name__}).",
                file=sys.stderr,
            )
            return 1
        print("Teams sign-in completed; the local token cache is ready.")
        return 0
    if args.command == "serve":
        from teams_exporter.local_server import serve_local_ui

        try:
            if args.unsecure:
                debug_path = TokenProvider(unsecure=True).cache_path
                print(TokenProvider.unsecure_warning(debug_path), file=sys.stderr)
            serve_local_ui(args.port, unsecure_tokens=args.unsecure)
        except ValueError as exc:
            parser.error(str(exc))
        return 0
    if args.command == "backup":
        try:
            service = TeamsService()
            destination = service.index.backup_to(
                args.destination, overwrite=args.overwrite
            )
        except AuthenticationRequiredError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:
            print(
                f"Archive backup failed ({exc.__class__.__name__}).",
                file=sys.stderr,
            )
            return 1
        if service.database_notice:
            print(f"Warning: {service.database_notice}", file=sys.stderr)
        print(f"Teams archive backup created: {destination}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
