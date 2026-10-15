"""Favorites: the Favorites sidebar folder marks chats only while TEAMS_CONNECTOR_FAVORITES is on.

Synthetic fixtures only.
"""

from __future__ import annotations

import pytest

from teams_connector.bootstrap import Services
from tests.fakes.teams_fake import FakeTeams

ONE = "19:one@unq.gbl.spaces"


async def test_the_favorites_folder_marks_chats_when_the_setting_is_on(
    services: Services, fake: FakeTeams, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEAMS_CONNECTOR_FAVORITES", "1")
    fake.favorite_ids = {ONE}

    page = await services.catalog.list(refresh=True, limit=100)

    flags = {chat.id: chat.favorite for chat in page.items}
    assert flags[ONE] is True
    assert any(flag is False for chat_id, flag in flags.items() if chat_id != ONE)


async def test_favorites_are_not_read_or_reported_when_the_setting_is_off(
    services: Services, fake: FakeTeams, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEAMS_CONNECTOR_FAVORITES", raising=False)
    fake.favorite_ids = {ONE}

    page = await services.catalog.list(refresh=True, limit=100)

    assert page.items and all(chat.favorite is None for chat in page.items)
    assert not any("conversationFolders" in call for call in fake.calls)
