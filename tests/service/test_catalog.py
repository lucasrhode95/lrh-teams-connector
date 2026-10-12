from __future__ import annotations

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest, NotFound
from tests.fakes.teams_fake import ME, PEER, FakeChannel, FakeTeams, cs_message


async def test_bootstrap_lists_chats_and_graph_channels_by_activity(
    services: Services, fake: FakeTeams
) -> None:
    page = await services.catalog.list(refresh=True)
    kinds = {c.id: c.kind for c in page.items}
    assert kinds == {
        "48:notes": "self",
        "19:general@thread.tacv2": "channel",
        "19:one@unq.gbl.spaces": "one_on_one",
        "19:group@thread.v2": "group",
        "19:meeting_abc@thread.v2": "meeting",
    }
    # most recent activity first; the self chat's activity comes from its history
    assert [c.id for c in page.items][:3] == ["48:notes", "19:general@thread.tacv2", "19:one@unq.gbl.spaces"]
    assert page.coverage.complete and page.coverage.source == "remote" and page.coverage.empty_excluded == 1


async def test_untitled_chats_are_named_from_people(services: Services) -> None:
    chats = {c.id: c for c in (await services.catalog.list(refresh=True)).items}
    assert chats["19:one@unq.gbl.spaces"].name == "Pat Peer"  # looked up in the directory
    assert chats["19:one@unq.gbl.spaces"].photo_user_id == PEER
    assert (
        chats["19:group@thread.v2"].name == "Pat Peer | Quinn Third +1"
    )  # sorted names; the user counts as +1
    assert chats["48:notes"].name == "Mia Example (You)" and chats["48:notes"].photo_user_id == ME
    channel = chats["19:general@thread.tacv2"]
    assert channel.name == "Platform · General" and channel.team_id == "team-1"
    assert channel.last_message_at is not None  # activity merged from the private catalog


async def test_delta_merges_changes_and_deletions(services: Services, fake: FakeTeams) -> None:
    await services.catalog.refresh()
    chat = fake.chats["19:meeting_abc@thread.v2"]
    chat.messages.append(cs_message("3002", "2026-09-30T09:00:00.000Z", "Follow-up"))
    fake.delta_rows = [chat.csa_row()]
    fake.deleted_chat_ids = ["19:group@thread.v2"]
    await services.catalog.refresh()
    chats = {c.id: c for c in services.store.chats()}
    assert "19:group@thread.v2" not in chats
    assert chats["19:meeting_abc@thread.v2"].last_message_preview == "Follow-up"
    assert chats["19:one@unq.gbl.spaces"].name == "Pat Peer"  # untouched rows keep their resolved names


async def test_expired_sync_token_restarts_from_a_snapshot(services: Services, fake: FakeTeams) -> None:
    await services.catalog.refresh()
    token = services.store.meta("csa_sync_token")
    assert token
    fake.expired_tokens.add(token)
    await services.catalog.refresh()
    assert services.store.meta("csa_sync_token") != token
    assert len(services.store.chats()) == 6  # five listed chats and the empty one


async def test_channel_missing_from_the_listing_is_found_by_id(services: Services, fake: FakeTeams) -> None:
    fake.channels["19:shared@thread.tacv2"] = FakeChannel(
        id="19:shared@thread.tacv2", team_id="team-1", name="Shared", listed=False, in_csa=True
    )
    await services.catalog.refresh()
    chat = services.store.chat("19:shared@thread.tacv2")
    assert chat is not None and chat.kind == "channel" and chat.team_id == "team-1"


async def test_channel_that_left_graph_is_removed(services: Services, fake: FakeTeams) -> None:
    await services.catalog.refresh()
    del fake.channels["19:general@thread.tacv2"]
    await services.catalog.refresh()
    assert services.store.chat("19:general@thread.tacv2") is None


async def test_partial_snapshot_is_completed_from_the_chat_service_listing(
    fake: FakeTeams, services: Services
) -> None:
    fake.csa_partial = True
    fake.chats["19:one@unq.gbl.spaces"].in_csa = False
    await services.catalog.refresh()
    chat = services.store.chat("19:one@unq.gbl.spaces")
    assert chat is not None and chat.name == "Pat Peer"  # named from its roster


async def test_empty_chats_and_kind_filters_and_paging(services: Services) -> None:
    first = await services.catalog.list(refresh=True, limit=2)
    assert len(first.items) == 2 and first.cursor
    rest = await services.catalog.list(cursor=first.cursor, limit=10)
    assert len(rest.items) == 3 and rest.cursor is None
    everything = await services.catalog.list(include_empty=True, limit=10)
    assert any(c.is_empty for c in everything.items)
    channels = await services.catalog.list(kind="channel")
    assert [c.kind for c in channels.items] == ["channel"]


async def test_list_answers_from_the_store_while_fresh(services: Services, fake: FakeTeams) -> None:
    await services.catalog.list(refresh=True)
    fake.calls.clear()
    page = await services.catalog.list()
    assert page.coverage.source == "local" and fake.calls == []


async def test_get_unknown_chat_is_not_found(services: Services) -> None:
    with pytest.raises(NotFound):
        await services.catalog.get("19:nope@thread.v2")


async def test_search_matches_names_and_indexed_participants(services: Services) -> None:
    await services.archive.sync(include_rosters=True, max_pages=1)
    found = await services.catalog.search("ray")
    assert {m.chat.id for m in found.items} == {"19:general@thread.tacv2"}
    match = found.items[0]
    assert not match.name_matched and match.matched_participants[0].name == "Ray Fourth"
    both = await services.catalog.search("quinn")
    assert both.items[0].name_matched and both.items[0].matched_participants[0].name == "Quinn Third"
    by_name = await services.catalog.search("sprint")
    assert by_name.items[0].name_matched and by_name.items[0].chat.id == "19:meeting_abc@thread.v2"
    assert found.coverage.rosters_indexed >= 4


async def test_search_chats_refresh_false_is_cache_only(services: Services, fake: FakeTeams) -> None:
    with pytest.raises(InvalidRequest, match="No cached chat catalog"):
        await services.catalog.search("sprint", refresh=False)
    assert fake.calls == []

    await services.catalog.list(refresh=True)
    services.store.set_meta(catalog_at="0")
    fake.calls.clear()
    found = await services.catalog.search("sprint", refresh=False)
    assert found.items and found.items[0].name_matched
    assert fake.calls == []


async def test_chat_search_reports_missing_and_unresolved_rosters(
    services: Services, fake: FakeTeams
) -> None:
    await services.catalog.list(refresh=True)
    missing = await services.catalog.search("no-synthetic-match")
    assert not missing.items
    assert missing.coverage.rosters_missing == missing.coverage.chats
    assert not missing.coverage.complete

    fake.chats["19:one@unq.gbl.spaces"].members = [
        ("33333333-3333-3333-3333-333333333333", ""),
        ("66666666-6666-6666-6666-666666666666", ""),
    ]
    await services.archive.sync(conversation_id="19:one@unq.gbl.spaces", max_pages=1)
    unresolved = await services.catalog.search("no-synthetic-match")
    assert unresolved.coverage.unresolved_participants == 1
    assert not unresolved.coverage.complete


async def test_bounded_archive_sync_rotates_between_chats(services: Services) -> None:
    first = await services.archive.sync(max_chats=1, max_pages=1, include_rosters=False, refresh_latest=False)
    second = await services.archive.sync(
        max_chats=1, max_pages=1, include_rosters=False, refresh_latest=False
    )
    assert len(first.items) == len(second.items) == 1
    assert first.items[0].conversation_id != second.items[0].conversation_id
