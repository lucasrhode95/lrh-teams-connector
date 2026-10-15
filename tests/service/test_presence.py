"""Presence of the other person in a one-on-one chat: the reported fields only, cached briefly, failing open.

Synthetic fixtures only.
"""

from __future__ import annotations

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest
from tests.fakes.teams_fake import FakeTeams

ONE = "19:one@unq.gbl.spaces"
CHANNEL = "19:general@thread.tacv2"


def presence_calls(fake: FakeTeams) -> int:
    return sum(1 for call in fake.calls if "presence.teams.microsoft.com" in call)


async def test_presence_reports_the_fields_the_connector_keeps(services: Services) -> None:
    result = await services.presence.for_chat(ONE)

    assert result.presence is not None and result.reason is None
    assert (result.presence.availability, result.presence.activity, result.presence.device_type) == (
        "Available",
        "InACall",
        "Desktop",
    )
    assert result.presence.last_active_at is not None
    dumped = str(result.model_dump(mode="json"))
    assert "calendar" not in dumped and "workLocation" not in dumped and "synthetic" not in dumped


async def test_a_successful_read_is_kept_for_a_minute(services: Services, fake: FakeTeams) -> None:
    await services.presence.for_chat(ONE)
    again = await services.presence.for_chat(ONE)

    assert again.presence is not None and presence_calls(fake) == 1


async def test_a_failed_read_fails_open_and_is_not_kept(services: Services, fake: FakeTeams) -> None:
    fake.presence_fails = True
    failed = await services.presence.for_chat(ONE)
    assert failed.presence is None and failed.reason is not None and "not read" in failed.reason

    after_failure = presence_calls(fake)
    fake.presence_fails = False
    recovered = await services.presence.for_chat(ONE)
    assert recovered.presence is not None and presence_calls(fake) > after_failure


async def test_only_one_to_one_chats_have_presence(services: Services) -> None:
    with pytest.raises(InvalidRequest, match="one-on-one"):
        await services.presence.for_chat(CHANNEL)
