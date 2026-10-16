from __future__ import annotations

from teams_connector.bootstrap import Services
from teams_connector.domain.models import ExportRequest
from tests.fakes.teams_fake import FakeTeams, add_meeting, cs_message, synthetic_drive

CHAT = "19:one@unq.gbl.spaces"
DRIVE = synthetic_drive(
    "11111111-2222-3333-4444-555555555555",
    "66666666-7777-8888-9999-aaaaaaaaaaaa",
    "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
)
SUMMARY = "Synthetic meeting summary."


def _request(**changes: object) -> ExportRequest:
    values: dict[str, object] = {"conversation_ids": [CHAT], "format": "txt"}
    values.update(changes)
    return ExportRequest.model_validate(values)


async def test_excerpt_is_the_latest_200_messages_and_entire_range_removes_only_preview_cap(
    services: Services, fake: FakeTeams
) -> None:
    fake.chats[CHAT].messages = [
        cs_message(
            f"m{index:03d}", f"2026-06-{1 + index // 24:02d}T{index % 24:02d}:00:00.000Z", f"Message {index}"
        )
        for index in range(210)
    ]
    await services.catalog.refresh()

    excerpt = await services.exports.preview(_request())
    complete = await services.exports.preview(_request(), entire_range=True)

    assert excerpt.messages == 200 and excerpt.capped
    assert "Message 10" in excerpt.text and "Message 9</" not in excerpt.text
    assert excerpt.text.index("Message 10") < excerpt.text.index("Message 209")
    assert complete.messages == 210 and not complete.capped
    assert complete.text.index("Message 0") < complete.text.index("Message 209")


async def test_excerpt_enforces_the_text_budget_without_splitting_utf8(
    services: Services, fake: FakeTeams
) -> None:
    fake.chats[CHAT].messages = [cs_message("long", "2026-06-01T10:00:00.000Z", "🙂漢字" * 300_000)]
    await services.catalog.refresh()

    preview = await services.exports.preview(_request())

    assert preview.capped
    assert len(preview.text.encode("utf-8")) <= 1024 * 1024
    assert "[Earlier text in this message omitted]" in preview.text


async def test_preview_includes_text_enrichment_without_fetching_transcript_files(
    services: Services, fake: FakeTeams
) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-04T10:00:00Z",
        drive=DRIVE,
        item="01SM1",
        transcript_id="t-01SM1",
        document={"entries": [], "events": []},
        summary=SUMMARY,
    )
    await services.catalog.refresh()

    preview = await services.exports.preview(_request())

    assert "[Transcript: Synthetic Standup]" in preview.text
    assert SUMMARY in preview.text
    assert fake.summary_reads
    assert not any("/media/transcripts/" in call for call in fake.calls)


async def test_preview_honors_user_message_limit_without_marking_it_as_truncated(
    services: Services, fake: FakeTeams
) -> None:
    fake.chats[CHAT].messages = [
        cs_message(
            f"m{index:03d}", f"2026-06-{1 + index // 24:02d}T{index % 24:02d}:00:00.000Z", f"Message {index}"
        )
        for index in range(210)
    ]
    await services.catalog.refresh()

    preview = await services.exports.preview(_request(message_limit=150))

    assert preview.messages == 150 and not preview.capped
    assert "Message 60" in preview.text and "Message 59</" not in preview.text
    assert "Message 209" in preview.text
