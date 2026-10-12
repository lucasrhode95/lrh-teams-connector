from __future__ import annotations

import json

from teams_connector.__main__ import EXIT_OK, build_parser, main
from tests.fakes.teams_fake import FakeTokens


def test_cli_has_one_canonical_name_and_no_legacy_application_commands() -> None:
    parser = build_parser()
    assert parser.prog == "teams-connector"
    assert parser.parse_args(["auth"]).command == "auth"
    assert parser.parse_args(["mcp"]).command == "mcp"
    assert parser.parse_args(["ui"]).command == "ui"
    assert parser.parse_args(["backup", "archive.sqlite3"]).command == "backup"


def test_status_reports_the_shared_profile_catalog_without_connecting(capsys) -> None:
    class Provider(FakeTokens):
        def __init__(self, **_: object) -> None:
            pass

    assert main(["status", "--json"], provider_factory=Provider) == EXIT_OK
    status = json.loads(capsys.readouterr().out)
    assert {profile["profile"] for profile in status["profiles"]} == {
        "outlook.graph",
        "outlook.api",
        "teams.spaces",
        "teams.csa",
        "teams.graph",
    }
