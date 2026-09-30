from __future__ import annotations

import argparse
import sys

from teams_exporter.teams_connector import TeamsConnector


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m teams_exporter")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("auth", help="complete the interactive Microsoft device-code sign-in")
    serve_parser = commands.add_parser("serve", help="serve the local conversation export UI")
    serve_parser.add_argument("--port", type=int, default=8765, help="loopback port (default: 8765)")
    args = parser.parse_args()
    if args.command == "auth":
        try:
            TeamsConnector().login(interactive=True)
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
            serve_local_ui(args.port)
        except ValueError as exc:
            parser.error(str(exc))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
