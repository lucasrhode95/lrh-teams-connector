from __future__ import annotations

import argparse
import sys

from teams_exporter.teams_connector import TeamsConnector


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m teams_exporter")
    parser.add_argument(
        "command",
        choices=("auth",),
        help="auth: complete the interactive Microsoft device-code sign-in",
    )
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
