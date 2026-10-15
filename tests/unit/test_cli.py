from __future__ import annotations

import json

from teams_connector.__main__ import EXIT_OK, build_parser, main
from tests.fakes.teams_fake import FakeTokens


def test_cli_name_and_commands() -> None:
    parser = build_parser()
    assert parser.prog == "teams-connector"
    assert parser.parse_args(["auth"]).command == "auth"
    assert parser.parse_args(["mcp"]).command == "mcp"
    assert parser.parse_args(["ui"]).command == "ui"


def test_status_reports_the_shared_profile_catalog_without_connecting(capsys) -> None:
    class Provider(FakeTokens):
        def __init__(self, **_: object) -> None:
            pass

    assert main(["status", "--json"], provider_factory=Provider) == EXIT_OK
    status = json.loads(capsys.readouterr().out)
    assert {profile["profile"] for profile in status["profiles"]} == {
        "outlook.graph",
        "outlook.api",
        "m365.search",
        "teams.spaces",
        "teams.csa",
        "teams.graph",
    }
    assert all(profile["client_credential_cached"] for profile in status["profiles"])
    assert all(profile["access_token_expires_on"] is None for profile in status["profiles"])
