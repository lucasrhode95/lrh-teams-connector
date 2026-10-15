from __future__ import annotations

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest, NotFound
from teams_connector.remote import graph_mapping
from tests.fakes.teams_fake import FakeTeams

ONE_ON_ONE = "19:one@unq.gbl.spaces"
CHANNEL = "19:general@thread.tacv2"
CONFIRMATION = "Synthetic authorization for a service test."


async def test_a_send_to_a_chat_reads_only_the_chat_delta(services: Services, fake: FakeTeams) -> None:
    await services.catalog.refresh()
    chat = services.store.chat(ONE_ON_ONE)
    assert chat is not None
    freshness = services.store.meta("catalog_at")
    fake.calls.clear()

    await services.sender.send(
        ONE_ON_ONE, expected_chat_name=chat.name, text="Synthetic text.", user_confirmation=CONFIRMATION
    )

    assert not any("/me/joinedTeams" in call or "allChannels" in call for call in fake.calls)
    assert services.store.meta("catalog_at") == freshness


async def test_a_send_to_a_channel_reads_its_team_and_that_channel_only(
    services: Services, fake: FakeTeams
) -> None:
    await services.catalog.refresh()
    channel = services.store.chat(CHANNEL)
    assert channel is not None
    fake.calls.clear()

    await services.sender.send(
        CHANNEL, expected_chat_name=channel.name, text="Synthetic text.", user_confirmation=CONFIRMATION
    )

    assert not any("allChannels" in call for call in fake.calls)
    reads = [call for call in fake.calls if call.startswith("GET ") and "/channels/" in call]
    assert len(reads) == 1 and CHANNEL in reads[0]


async def test_a_renamed_channel_is_checked_against_its_current_name(
    services: Services, fake: FakeTeams
) -> None:
    await services.catalog.refresh()
    stored = services.store.chat(CHANNEL)
    assert stored is not None
    fake.channels[CHANNEL].name = "Renamed"
    renamed = graph_mapping.channel(
        {"id": "team-1", "displayName": "Platform"}, {"id": CHANNEL, "displayName": "Renamed"}
    )

    with pytest.raises(InvalidRequest, match="does not match"):
        await services.sender.send(
            CHANNEL, expected_chat_name=stored.name, text="Synthetic text.", user_confirmation=CONFIRMATION
        )
    result = await services.sender.send(
        CHANNEL, expected_chat_name=renamed.name, text="Synthetic text.", user_confirmation=CONFIRMATION
    )
    assert result.chat_name == renamed.name


async def test_a_channel_that_is_gone_is_refused_after_the_full_refresh(
    services: Services, fake: FakeTeams
) -> None:
    await services.catalog.refresh()
    channel = services.store.chat(CHANNEL)
    assert channel is not None
    del fake.channels[CHANNEL]

    with pytest.raises(NotFound):
        await services.sender.send(
            CHANNEL, expected_chat_name=channel.name, text="Synthetic text.", user_confirmation=CONFIRMATION
        )
    assert services.store.chat(CHANNEL) is None
