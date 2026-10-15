"""Meeting notes from Loop pages: a marker when reading, the page's content in exports and downloads.

Synthetic fixtures and the fake tenant only.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import Upstream
from teams_connector.domain.models import ExportRequest
from teams_connector.service.export import orchestrator
from tests.fakes.teams_fake import FakeTeams, cs_message

MEETING = "19:meeting_abc@thread.v2"
LINK = "https://tenant-my.sharepoint.com/:fl:/g/personal/organizer/sample-page?e=abc"
EMBED = (
    f'Everyone, that is a wrap. <a href="{LINK}" itemtype="http://schema.skype.com/FluidAutoEmbedLink" '
    'itemid="00000000-0000-0000-0000-000000000001"></a>'
)
PAGE = (Path(__file__).parents[1] / "fixtures" / "loop_meeting_notes.html").read_text(encoding="utf-8")


def bot_message(message_id: str, when: str, *, meeting_ended: bool = True) -> dict[str, Any]:
    """The Facilitator app's message; Chat Service sends its bot metadata as JSON inside strings."""
    record = cs_message(message_id, when, EMBED, author="28:facilitator-app", name="Facilitator")
    if meeting_ended:
        skill = json.dumps({"eventType": "TM_MeetingEnded", "callId": "synthetic-call"})
        record["properties"]["botMetadata"] = json.dumps({"aiMetadata": {"botAiSkill": skill}})
    return record


@pytest.fixture
def notes_message(fake: FakeTeams) -> None:
    fake.chats[MEETING].messages.append(bot_message("3002", "2026-07-01T10:00:00.000Z"))
    fake.loop_pages[LINK] = PAGE


async def test_reading_shows_a_marker_and_fetches_nothing(
    services: Services, fake: FakeTeams, notes_message: None
) -> None:
    page = await services.history.get_messages(MEETING)

    message = next(m for m in page.items if m.id == "3002")
    assert message.text == f"[Meeting notes] {LINK}"
    assert [(a.name, a.source) for a in message.attachments] == [("Meeting notes.txt", "loop")]
    assert fake.loop_reads == []


async def test_other_loop_links_keep_their_text(services: Services, fake: FakeTeams) -> None:
    fake.chats[MEETING].messages += [
        cs_message("3003", "2026-07-01T11:00:00.000Z", f"See {EMBED}"),  # a person's
        bot_message("3005", "2026-07-01T11:30:00.000Z", meeting_ended=False),  # an app's, not at meeting end
    ]

    page = await services.history.get_messages(MEETING)

    for message_id in ("3003", "3005"):
        message = next(m for m in page.items if m.id == message_id)
        assert "Everyone, that is a wrap." in message.text and message.attachments == []


async def test_download_saves_the_notes_as_text_read_now(
    services: Services, fake: FakeTeams, notes_message: None
) -> None:
    first = await services.files.download_attachments(MEETING, message_id="3002", sent_at=None, names=None)
    fake.loop_pages[LINK] = PAGE.replace("Friday", "Monday")  # edited after the first read
    second = await services.files.download_attachments(
        MEETING, message_id="3002", sent_at=None, names=["Meeting notes.txt"]
    )

    assert fake.loop_reads == [LINK, LINK]
    [saved] = second.files
    text = Path(saved.path).read_text(encoding="utf-8")
    assert text.startswith("Meeting notes: Weekly sync 2026-10-14 (read from the live Loop page at ")
    assert "later edits are not included" in text and "ship on Monday" in text
    assert "AI-generated" not in text and "Task list category" not in text
    assert "- Draft the release notes (assigned to Alex Doe, due 10/17/2026)" in text
    assert first.files[0].name == "Meeting notes.txt"


async def test_an_export_puts_the_notes_after_the_message_reading_each_page_once(
    services: Services, fake: FakeTeams, notes_message: None
) -> None:
    fake.chats[MEETING].messages.append(bot_message("3004", "2026-07-01T12:00:00.000Z"))

    artifact = await services.exports.export(ExportRequest(conversation_ids=[MEETING]))

    text = Path(artifact.path).read_text(encoding="utf-8")
    assert f"[Meeting notes] {LINK}\nMeeting notes: Weekly sync 2026-10-14 (read from the live" in text
    assert "# Follow-up tasks\n- Draft the release notes" in text
    assert fake.loop_reads == [LINK]
    assert artifact.chats[0].attachments_downloaded == 1
    assert artifact.path.endswith(".txt")  # nothing beside the text, so no ZIP


async def test_an_unreadable_page_is_reported_like_an_unavailable_attachment(
    services: Services, fake: FakeTeams, notes_message: None
) -> None:
    fake.loop_pages[LINK] = 403

    artifact = await services.exports.export(ExportRequest(conversation_ids=[MEETING]))

    text = Path(artifact.path).read_text(encoding="utf-8")
    assert f"[Attachment unavailable: Meeting notes.txt] {LINK}" in text
    assert artifact.chats[0].attachments_failed == 1
    with pytest.raises(Upstream, match="denied access"):
        await services.files.download_attachments(MEETING, message_id="3002", sent_at=None, names=None)


async def test_pages_beyond_the_attachment_limit_are_not_read(
    services: Services, fake: FakeTeams, notes_message: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = LINK.replace("sample-page", "second-page")
    fake.loop_pages[second] = PAGE
    record = bot_message("3004", "2026-07-01T12:00:00.000Z")
    record["content"] = record["content"].replace(LINK, second)
    fake.chats[MEETING].messages.append(record)
    monkeypatch.setattr(orchestrator, "MAX_ASSETS_PER_CONVERSATION", 1)

    artifact = await services.exports.export(ExportRequest(conversation_ids=[MEETING]))

    assert fake.loop_reads == [LINK]  # the second page is past the limit
    text = Path(artifact.path).read_text(encoding="utf-8")
    assert f"[Attachment unavailable: Meeting notes.txt] {second}" in text


async def test_without_attachments_the_export_reads_no_page(
    services: Services, fake: FakeTeams, notes_message: None
) -> None:
    artifact = await services.exports.export(
        ExportRequest(conversation_ids=[MEETING], include_attachments=False)
    )

    assert f"[Meeting notes] {LINK}" in Path(artifact.path).read_text(encoding="utf-8")
    assert fake.loop_reads == []
    assert not zipfile.is_zipfile(artifact.path)
