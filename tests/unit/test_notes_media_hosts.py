"""The notes media hosts: the default for this deployment, the environment override, and refusals.

Synthetic values only."""

from __future__ import annotations

import pytest

from teams_connector import config
from teams_connector.domain.errors import ConfigurationError


def test_the_default_is_this_deployments_media_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS", raising=False)

    assert config.notes_media_hosts() == frozenset(config.DEFAULT_NOTES_MEDIA_HOSTS)


def test_an_empty_value_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS", " , ")

    assert config.notes_media_hosts() == frozenset(config.DEFAULT_NOTES_MEDIA_HOSTS)


def test_the_environment_replaces_the_default_with_the_named_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS", " Synthetic-A.example.invalid, synthetic-b.example.invalid "
    )

    assert config.notes_media_hosts() == frozenset(
        {"synthetic-a.example.invalid", "synthetic-b.example.invalid"}
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://synthetic.example.invalid",
        "bad host.example.invalid",
        "no-dot",
        "synthetic.example.invalid/path",
        "user@synthetic.example.invalid",
    ],
)
def test_a_value_that_is_not_a_host_name_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS", value)

    with pytest.raises(ConfigurationError):
        config.notes_media_hosts()
