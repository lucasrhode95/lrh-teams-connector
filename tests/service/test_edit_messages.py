from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import (
    ConnectorError,
    InvalidRequest,
    WriteOutcomeUnknown,
)
from tests.fakes.teams_fake import ME, FakeTeams, cs_message

ONE = "19:one@unq.gbl.spaces"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-image"


async def test_server_markup_and_timestamp_normalization_is_verified(
    services: Services, fake: FakeTeams, monkeypatch
) -> None:
    handle = fake._chat_service

    def normalized(request, path, query):
        response = handle(request, path, query)
        if request.method == "PUT" and response.is_success:
            row = next(m for m in fake.chats[ONE].messages if m["id"] == "1002")
            row["content"] = row["content"].replace("<b>", "<strong>").replace("</b>", "</strong>")
            row["properties"]["edittime"] = str(
                int((datetime.now(UTC) + timedelta(seconds=1)).timestamp() * 1000)
            )
        return response

    monkeypatch.setattr(fake, "_chat_service", normalized)
    result = await services.editor.edit(
        ONE, expected_chat_name="Pat Peer", message_id="1002", html="<b>Revised</b>"
    )
    assert result.verified


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path]:
    document, image = tmp_path / "plan.txt", tmp_path / "plot.png"
    document.write_text("Synthetic plan", encoding="utf-8")
    image.write_bytes(PNG)
    return document, image


async def test_plain_and_html_edits_keep_id_sender_and_arrival(services: Services, fake: FakeTeams) -> None:
    before = copy.deepcopy(next(m for m in fake.chats[ONE].messages if m["id"] == "1002"))
    first = await services.editor.edit(
        ONE, expected_chat_name="Pat Peer", message_id="1002", text="A < B\nnext"
    )
    assert first.verified and first.status == "updated" and first.edited_at
    payload = fake.edits[-1][1]
    assert payload["content"] == "<p>A &lt; B<br>next</p>"
    for key in ("id", "from", "originalarrivaltime", "sequenceId"):
        assert payload[key] == before[key]
    second = await services.editor.edit(
        ONE, expected_chat_name="Pat Peer", message_id="1002", html="<b>Revised</b>"
    )
    assert second.verified
    assert fake.edits[-1][1]["content"] == "<b>Revised</b>"


async def test_add_preserve_replace_and_remove_attachments(
    services: Services, fake: FakeTeams, inputs
) -> None:
    document, image = inputs
    result = await services.editor.edit(
        ONE,
        expected_chat_name="Pat Peer",
        message_id="1002",
        files=[str(document)],
        images=[str(image)],
    )
    assert result.verified
    assert {a.kind for a in result.attachments} == {"file", "image"}
    assert fake.uploads[-1][2] == b"Synthetic plan"
    assert fake.invites[-1][1]["recipients"]  # shared before the edit
    image_name = next(a.name for a in result.attachments if a.kind == "image")
    file_name = next(a.name for a in result.attachments if a.kind == "file")
    assert fake.images[next(iter(fake.ams_created))] == PNG
    result = await services.editor.edit(
        ONE, expected_chat_name="Pat Peer", message_id="1002", text="New caption"
    )
    assert result.verified and len(result.attachments) == 2
    result = await services.editor.edit(
        ONE,
        expected_chat_name="Pat Peer",
        message_id="1002",
        remove_attachments=[file_name],
        files=[str(document)],
    )
    assert result.verified and len(result.attachments) == 2
    assert image_name in {a.name for a in result.attachments}
    result = await services.editor.edit(
        ONE,
        expected_chat_name="Pat Peer",
        message_id="1002",
        remove_attachments=[a.name for a in result.attachments],
    )
    assert result.verified and result.attachments == []
    assert fake.deleted_items == [] and fake.ams_deleted == []  # references removed, sources retained


async def test_image_named_in_html_is_not_duplicated(services: Services, fake: FakeTeams, inputs) -> None:
    _, image = inputs
    result = await services.editor.edit(
        ONE,
        expected_chat_name="Pat Peer",
        message_id="1002",
        html='<p><img src="plot.png"></p>',
        images=[str(image)],
    )
    assert result.verified and len(result.attachments) == 1
    assert fake.edits[-1][1]["content"].count("<img") == 1


@pytest.mark.parametrize("failure", ["401", "403", "429", "503", "504", "network", "connect"])
async def test_edit_write_is_never_repeated_and_uncertain_uploads_stay(
    services: Services,
    fake: FakeTeams,
    inputs,
    failure: str,
) -> None:
    document, image = inputs
    fake.fail_edits = failure
    error = WriteOutcomeUnknown if failure in ("504", "network") else ConnectorError
    with pytest.raises(error):
        await services.editor.edit(
            ONE,
            expected_chat_name="Pat Peer",
            message_id="1002",
            text="Changed",
            files=[str(document)],
            images=[str(image)],
        )
    assert len(fake.edits) == 1
    if failure in ("504", "network"):
        assert fake.deleted_items == [] and fake.ams_deleted == []
    else:
        assert len(fake.deleted_items) == 1 and len(fake.ams_deleted) == 1


async def test_failed_readback_is_accepted_without_rollback(
    services: Services, fake: FakeTeams, inputs
) -> None:
    document, image = inputs
    fake.fail_edit_readback = True
    result = await services.editor.edit(
        ONE,
        expected_chat_name="Pat Peer",
        message_id="1002",
        files=[str(document)],
        images=[str(image)],
    )
    assert not result.verified and result.status == "accepted" and result.reason
    assert len(fake.edits) == 1 and fake.deleted_items == [] and fake.ams_deleted == []


@pytest.mark.parametrize("message_id", ["1003", "deleted", "event"])
async def test_other_users_deleted_and_system_messages_are_not_edited(
    services: Services, fake: FakeTeams, inputs, message_id
) -> None:
    fake.chats[ONE].messages.extend(
        [
            cs_message("deleted", "2026-09-05T10:00:00Z", author=ME, deleted="2026-09-05T11:00:00Z"),
            cs_message("event", "2026-09-05T10:00:00Z", author=ME, kind="ThreadActivity/AddMember"),
        ]
    )
    with pytest.raises(InvalidRequest):
        await services.editor.edit(ONE, expected_chat_name="Pat Peer", message_id=message_id, text="Changed")
    assert fake.edits == [] and fake.uploads == []


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"text": "a", "html": "<b>b</b>"},
        {"text": ""},
        {"text": "x" * 10001},
        {"html": "<script>x</script>"},
        {"html": '<img src="https://other.example/image.png">'},
        {"remove_attachments": ["missing.txt"]},
        {"remove_attachments": [" "]},
        {"files": ["relative.txt"]},
        {"html": '<attachment id="fabricated"></attachment>'},
    ],
)
async def test_invalid_edits_never_upload_or_write(services: Services, fake: FakeTeams, args) -> None:
    with pytest.raises(InvalidRequest):
        await services.editor.edit(ONE, expected_chat_name="Pat Peer", message_id="1002", **args)
    assert fake.edits == [] and fake.uploads == [] and fake.ams_created == []


async def test_wrong_chat_name_and_channels_are_refused(services: Services, fake: FakeTeams) -> None:
    with pytest.raises(InvalidRequest, match="does not match"):
        await services.editor.edit(ONE, expected_chat_name="Wrong", message_id="1002", text="Changed")
    await services.catalog.refresh()
    channel = services.store.chat("19:general@thread.tacv2")
    assert channel
    with pytest.raises(InvalidRequest, match="not in channels"):
        await services.editor.edit(
            channel.id, expected_chat_name=channel.name, message_id="5001", text="Changed"
        )
    assert fake.edits == []


async def test_concurrent_change_aborts_before_put(services: Services, fake: FakeTeams, monkeypatch) -> None:
    source = services.editor._editor
    prepare = source.prepare

    async def changed(chat, message_id):
        target = await prepare(chat, message_id)
        next(m for m in fake.chats[ONE].messages if m["id"] == message_id)["content"] = "<p>Human change</p>"
        return target

    monkeypatch.setattr(source, "prepare", changed)
    with pytest.raises(InvalidRequest, match="changed while"):
        await services.editor.edit(ONE, expected_chat_name="Pat Peer", message_id="1002", text="Changed")
    assert fake.edits == []


async def test_share_failure_rolls_back_new_upload_and_leaves_original(
    services: Services, fake: FakeTeams, inputs
) -> None:
    document, _ = inputs
    fake.fail_invites = True
    with pytest.raises(ConnectorError):
        await services.editor.edit(
            ONE, expected_chat_name="Pat Peer", message_id="1002", files=[str(document)]
        )
    assert fake.edits == [] and len(fake.deleted_items) == 1


async def test_self_chat_edit_uses_no_sharing_invite(services: Services, fake: FakeTeams, inputs) -> None:
    await services.catalog.refresh()
    chat = services.store.chat("48:notes")
    assert chat
    document, _ = inputs
    result = await services.editor.edit(
        chat.id, expected_chat_name=chat.name, message_id="4001", files=[str(document)]
    )
    assert result.verified and fake.invites == []
