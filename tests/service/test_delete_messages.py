"""Deleting the user's own messages: each re-read, only the user's own deleted, each delete sent once.

Synthetic fixtures and the fake tenant only.
"""

from __future__ import annotations

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import AuthenticationRequired, InvalidRequest
from teams_connector.service import delete
from tests.fakes.teams_fake import ME, ME_NAME, FakeTeams, cs_message

ONE = "19:one@unq.gbl.spaces"


async def test_only_the_users_own_messages_are_deleted(services: Services, fake: FakeTeams) -> None:
    fake.chats[ONE].messages.append(
        cs_message(
            "1004",
            "2026-09-04T10:00:00.000Z",
            "Sent with a file",
            author=ME,
            name=ME_NAME,
            files=[
                {"fileName": "plan.pdf", "fileInfo": {"shareUrl": "https://tenant.sharepoint.com/s/plan"}},
                {"fileName": "linkless.txt"},  # no link to follow, but still in OneDrive
            ],
        )
    )

    result = await services.deleter.delete(
        ONE, expected_chat_name="Pat Peer", message_ids=["1002", "1003", "1004", "1002"]
    )

    assert fake.deletes == [(ONE, "1002"), (ONE, "1004")]
    assert [(m.message_id, m.files) for m in result.deleted] == [
        ("1002", []),
        ("1004", ["plan.pdf", "linkless.txt"]),
    ]
    assert [(m.message_id, m.reason) for m in result.skipped] == [("1003", "Not your message.")]
    assert result.uncertain == []

    page = await services.history.get_messages(ONE)
    assert {m.id for m in page.items if m.is_deleted} == {"1002", "1004"}


async def test_an_already_deleted_or_unknown_message_is_skipped(services: Services, fake: FakeTeams) -> None:
    await services.deleter.delete(ONE, expected_chat_name="Pat Peer", message_ids=["1002"])

    result = await services.deleter.delete(ONE, expected_chat_name="Pat Peer", message_ids=["1002", "9999"])

    assert result.deleted == []
    assert [m.message_id for m in result.skipped] == ["1002", "9999"]
    assert result.skipped[0].reason == "Already deleted."
    assert result.skipped[1].reason.startswith("Not read:")
    assert fake.deletes == [(ONE, "1002")]


async def test_an_unconfirmed_delete_is_uncertain_and_not_retried(
    services: Services, fake: FakeTeams
) -> None:
    fake.fail_deletes = {"1002": "network"}

    result = await services.deleter.delete(ONE, expected_chat_name="Pat Peer", message_ids=["1002"])

    assert [m.message_id for m in result.uncertain] == ["1002"] and result.deleted == []
    assert "may or may not have been applied" in result.uncertain[0].reason
    assert fake.deletes == [(ONE, "1002")]


async def test_a_refused_delete_is_skipped_and_the_rest_continue(services: Services, fake: FakeTeams) -> None:
    fake.chats[ONE].messages.append(
        cs_message("1005", "2026-09-05T10:00:00.000Z", "Another of mine", author=ME, name=ME_NAME)
    )
    fake.fail_deletes = {"1002": "403"}

    result = await services.deleter.delete(ONE, expected_chat_name="Pat Peer", message_ids=["1002", "1005"])

    assert result.skipped[0].message_id == "1002" and result.skipped[0].reason.startswith("Teams refused:")
    assert [m.message_id for m in result.deleted] == ["1005"]


async def test_a_sign_in_failure_midway_reports_what_was_done(services: Services, fake: FakeTeams) -> None:
    fake.chats[ONE].messages.append(
        cs_message("1005", "2026-09-05T10:00:00.000Z", "Another of mine", author=ME, name=ME_NAME)
    )
    fake.fail_deletes = {"1005": "401"}

    result = await services.deleter.delete(ONE, expected_chat_name="Pat Peer", message_ids=["1002", "1005"])

    assert [m.message_id for m in result.deleted] == ["1002"]
    assert [m.message_id for m in result.skipped] == ["1005"]
    assert result.skipped[0].reason.startswith("Not attempted:")


async def test_a_sign_in_failure_before_any_delete_is_raised(services: Services, fake: FakeTeams) -> None:
    fake.fail_deletes = {"1002": "401"}

    with pytest.raises(AuthenticationRequired):
        await services.deleter.delete(ONE, expected_chat_name="Pat Peer", message_ids=["1002"])


async def test_the_self_chat_messages_can_be_deleted(services: Services, fake: FakeTeams) -> None:
    await services.catalog.refresh()
    chat = services.store.chat("48:notes")
    assert chat is not None

    result = await services.deleter.delete("48:notes", expected_chat_name=chat.name, message_ids=["4001"])

    assert [m.message_id for m in result.deleted] == ["4001"]


@pytest.mark.parametrize(
    ("conversation_id", "name", "ids", "message"),
    [
        (ONE, "Someone Else", ["1002"], "does not match"),
        (ONE, "Pat Peer", [], "Pass the ids"),
        (ONE, "Pat Peer", ["  "], "non-empty"),
        (ONE, "Pat Peer", [str(n) for n in range(delete.MAX_DELETES + 1)], "at most"),
        ("19:general@thread.tacv2", "General", ["5001"], "not in channels"),
    ],
)
async def test_bad_requests_delete_nothing(
    services: Services, fake: FakeTeams, conversation_id: str, name: str, ids: list[str], message: str
) -> None:
    await services.catalog.refresh()
    if conversation_id != ONE:
        chat = services.store.chat(conversation_id)
        assert chat is not None
        name = chat.name

    with pytest.raises(InvalidRequest, match=message):
        await services.deleter.delete(conversation_id, expected_chat_name=name, message_ids=ids)
    assert fake.deletes == []
