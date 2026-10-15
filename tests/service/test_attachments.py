"""Attachments by name: pasted images and shared files, named from their message and fetched once per source.

Synthetic fixtures only: the object ids, hosts and bytes are invented.
"""

from __future__ import annotations

import base64
import hashlib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest, NotFound
from teams_connector.domain.models import ExportRequest
from tests.fakes.teams_fake import FakeTeams, cs_message, graph_message

ONE = "19:one@unq.gbl.spaces"
CHANNEL = "19:general@thread.tacv2"
OBJECT = "0-neu-d1-0123456789abcdef0123456789abcdef"
PICTURE = b"\x89PNG\r\n\x1a\nsynthetic picture"
AMS_TAG = (
    f'<img src="https://eu-api.asm.skype.com/v1/objects/{OBJECT}/views/imgo" '
    f'itemtype="http://schema.skype.com/AMSImage" itemscope="png" itemid="{OBJECT}" alt="image">'
)
WHEN = "2026-09-05T10:00:00.000Z"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def add_picture_message(fake: FakeTeams, message_id: str = "2001", when: str = WHEN) -> None:
    fake.images[OBJECT] = PICTURE
    fake.chats[ONE].messages.append(cs_message(message_id, when, f"Look {AMS_TAG}"))


def calls_to(fake: FakeTeams, host: str) -> int:
    return sum(1 for call in fake.calls if host in call)


async def test_pasted_image_is_named_by_its_object_and_fetched_once(
    services: Services, fake: FakeTeams
) -> None:
    add_picture_message(fake)

    (saved,) = (
        await services.files.download_attachments(ONE, message_id="2001", sent_at=None, names=None)
    ).files
    assert saved.name == f"{OBJECT}.png" and Path(saved.path).read_bytes() == PICTURE
    assert saved.content_type == "image/png"
    assert calls_to(fake, "asm.skype.com") == 1

    (again,) = (
        await services.files.download_attachments(ONE, message_id="2001", sent_at=None, names=None)
    ).files
    assert again.path == saved.path and calls_to(fake, "asm.skype.com") == 1


async def test_a_message_is_found_by_its_arrival_time(services: Services, fake: FakeTeams) -> None:
    add_picture_message(fake)

    downloaded = await services.files.download_attachments(
        ONE, message_id=None, sent_at=at("2026-09-05T10:00:00"), names=None
    )
    assert [saved.name for saved in downloaded.files] == [f"{OBJECT}.png"]


async def test_messages_sharing_one_arrival_time_are_refused_with_their_ids(
    services: Services, fake: FakeTeams
) -> None:
    fake.chats[ONE].messages += [cs_message("2101", WHEN, "one"), cs_message("2102", WHEN, "two")]

    with pytest.raises(InvalidRequest, match="'2101'"):
        await services.files.download_attachments(ONE, message_id=None, sent_at=at(WHEN), names=None)


async def test_names_select_files_and_an_unknown_name_is_refused(services: Services, fake: FakeTeams) -> None:
    add_picture_message(fake)

    with pytest.raises(NotFound, match="'notes.txt'"):
        await services.files.download_attachments(ONE, message_id="2001", sent_at=None, names=["notes.txt"])
    picked = await services.files.download_attachments(
        ONE, message_id="2001", sent_at=None, names=[f"{OBJECT}.png"]
    )
    assert [saved.name for saved in picked.files] == [f"{OBJECT}.png"]


async def test_a_shared_file_is_kept_by_its_drive_item_and_not_fetched_again(
    services: Services, fake: FakeTeams
) -> None:
    first = await services.files.download_attachments(ONE, message_id="1003", sent_at=None, names=None)
    fetched = calls_to(fake, "sharepoint.com")

    second = await services.files.download_attachments(ONE, message_id="1003", sent_at=None, names=None)
    assert second.files[0].path == first.files[0].path
    assert calls_to(fake, "sharepoint.com") == fetched


async def test_a_data_image_needs_no_request_to_save(services: Services, fake: FakeTeams) -> None:
    source = "data:image/png;base64," + base64.b64encode(PICTURE).decode()
    fake.chats[ONE].messages.append(cs_message("2201", "2026-09-06T10:00:00.000Z", f'<img src="{source}">'))
    before = len(fake.calls)

    (saved,) = (
        await services.files.download_attachments(ONE, message_id="2201", sent_at=None, names=None)
    ).files
    digest = hashlib.sha256(PICTURE).hexdigest()
    assert saved.name == f"sha-{digest[:8]}.png" and Path(saved.path).read_bytes() == PICTURE
    assert not any("asm.skype.com" in call or "sharepoint.com" in call for call in fake.calls[before:])


async def test_channel_hosted_copy_is_named_by_its_object_and_read_through_graph(
    services: Services, fake: FakeTeams
) -> None:
    fake.hosted["h1"] = PICTURE
    hosted = (
        f'<img src="https://graph.microsoft.com/v1.0/teams/team-1/channels/{CHANNEL}/messages/5010/'
        f'hostedContents/h1/$value" itemid="{OBJECT}" alt="image">'
    )
    fake.channels[CHANNEL].threads.append(graph_message("5010", "2026-09-05T11:00:00.000Z", f"Shot {hosted}"))

    (saved,) = (
        await services.files.download_attachments(CHANNEL, message_id="5010", sent_at=None, names=None)
    ).files
    assert saved.name == f"{OBJECT}.png" and Path(saved.path).read_bytes() == PICTURE


async def test_export_names_pasted_images_and_writes_them_beside_the_text(
    services: Services, fake: FakeTeams
) -> None:
    add_picture_message(fake)

    artifact = await services.exports.export(ExportRequest(conversation_ids=[ONE]))

    with zipfile.ZipFile(artifact.path) as archive:
        names = archive.namelist()
        picture = next(name for name in names if name.endswith(f"/{OBJECT}.png"))
        assert archive.read(picture) == PICTURE
        text = archive.read(next(name for name in names if name.endswith(".txt"))).decode("utf-8")
    assert f"[Image: {OBJECT}.png]" in text and "asm.skype.com" not in text


async def test_export_without_attachments_keeps_only_the_markers(services: Services, fake: FakeTeams) -> None:
    add_picture_message(fake)

    artifact = await services.exports.export(ExportRequest(conversation_ids=[ONE], include_attachments=False))

    text = Path(artifact.path).read_text(encoding="utf-8")
    assert f"[Image: {OBJECT}.png]" in text and calls_to(fake, "asm.skype.com") == 0
