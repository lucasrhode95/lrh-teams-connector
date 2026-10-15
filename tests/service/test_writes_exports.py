from __future__ import annotations

import asyncio
import json
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest, NotFound, WriteOutcomeUnknown
from teams_connector.domain.models import ExportProgress, ExportRequest
from teams_connector.service import localfiles
from teams_connector.service.export import orchestrator, packaging
from tests.fakes.teams_fake import FakeTeams, cs_message

ONE = "19:one@unq.gbl.spaces"
CHANNEL = "19:general@thread.tacv2"


async def test_send_checks_the_live_name_and_posts_escaped_html(services: Services, fake: FakeTeams) -> None:
    result = await services.sender.send(ONE, expected_chat_name=" pat  PEER ", text="a < b\nline 2")
    path, body = fake.sent[0]
    assert path == "/chats/19:one@unq.gbl.spaces/messages"
    assert body == {"body": {"contentType": "html", "content": "<p>a &lt; b<br>line 2</p>"}}
    assert result.message_id and result.sent_at and result.chat_name == "Pat Peer" and result.characters == 12


async def test_channel_send_posts_to_the_team_channel(services: Services, fake: FakeTeams) -> None:
    await services.sender.send(CHANNEL, expected_chat_name="Platform · General", text="hi")
    assert fake.sent[0][0] == "/teams/team-1/channels/19:general@thread.tacv2/messages"


async def test_send_refusals_never_reach_teams(services: Services, fake: FakeTeams) -> None:
    with pytest.raises(InvalidRequest, match="does not match"):
        await services.sender.send(ONE, expected_chat_name="Someone else", text="hi")
    with pytest.raises(InvalidRequest, match="10,000"):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", text="x" * 10_001)
    with pytest.raises(NotFound):
        await services.sender.send("19:nope@thread.v2", expected_chat_name="x", text="hi")
    assert fake.sent == []


@pytest.mark.parametrize("failure", ["network", "504"])
async def test_ambiguous_send_is_reported_and_not_retried(
    services: Services, fake: FakeTeams, failure: str
) -> None:
    fake.fail_sends = failure
    with pytest.raises(WriteOutcomeUnknown):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", text="hi")
    assert sum(c.startswith("POST graph.microsoft.com/v1.0/chats") for c in fake.calls) == 1


async def test_single_chat_export_is_one_txt(services: Services) -> None:
    progress: list[ExportProgress] = []
    artifact = await services.exports.export(
        ExportRequest(conversation_ids=[ONE], include_attachments=False), progress=progress.append
    )
    text = Path(artifact.path).read_text(encoding="utf-8")
    assert artifact.filename.endswith(".txt") and artifact.message_count == 3
    assert "Conversation: Pat Peer" in text and "Time zone: UTC" in text
    assert "[2026-09-02T10:00:00.000Z] Mia Example\nSecond, about the budget review" in text
    assert text.index("First hello") < text.index("Third")
    assert progress[-1].phase == "done" and progress[0].phase == "retrieving"


async def test_single_chat_with_attachment_is_a_zip_with_text_and_attachment_together(
    services: Services,
) -> None:
    artifact = await services.exports.export(ExportRequest(conversation_ids=[ONE]))
    assert artifact.filename.endswith(".zip")
    assert artifact.chats[0].attachments_downloaded == 1
    with zipfile.ZipFile(artifact.path) as archive:
        names = archive.namelist()
        text_name = next(name for name in names if name.endswith(".txt"))
        text_folder = text_name.rpartition("/")[0]
        assert text_folder and f"{text_folder}/notes.txt" in names
        assert all(name.startswith(f"{text_folder}/") for name in names)
        text = archive.read(text_name).decode("utf-8")
    assert "First hello" in text and "notes.txt" in text
    assert "https://tenant.sharepoint.com/s/notes" not in text


async def test_selected_messages_without_attachments_remain_a_plain_file(services: Services) -> None:
    when = datetime(2026, 9, 2, 10, tzinfo=UTC)
    artifact = await services.exports.export(ExportRequest(conversation_ids=[ONE], since=when, until=when))
    assert artifact.filename.endswith(".txt")
    assert "Second, about the budget review" in Path(artifact.path).read_text(encoding="utf-8")


async def test_unavailable_attachment_keeps_export_and_reports_failure(
    services: Services, fake: FakeTeams
) -> None:
    fake.files.clear()
    artifact = await services.exports.export(ExportRequest(conversation_ids=[ONE]))
    assert artifact.filename.endswith(".zip")
    assert artifact.chats[0].attachments_failed == 1
    with zipfile.ZipFile(artifact.path) as archive:
        text = next(
            archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".txt")
        )
    assert "[Attachment unavailable: notes.txt]" in text


async def test_marker_rewriting_keeps_jsonl_valid_for_special_names(tmp_path: Path) -> None:
    name = 'quote"\nline.png'
    marker = f"[Image: {name}]"
    path = tmp_path / "synthetic.jsonl"
    path.write_text(
        json.dumps({"text": f"{marker} [File: {name}]"}, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    orchestrator._mark_unavailable(path, [name])

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    replacement = f"[Attachment unavailable: {name}]"
    assert records == [{"text": f"{replacement} {replacement}"}]


async def test_attachment_name_cannot_overwrite_the_conversation_export(
    services: Services, fake: FakeTeams
) -> None:
    chat = await services.catalog.get(ONE)
    transcript_name = f"{packaging.filename_stem(chat)}.txt"
    fake.files["https://tenant.sharepoint.com/s/notes"] = (transcript_name, b"attachment")
    message = next(m for m in fake.chats[ONE].messages if m["id"] == "1003")
    files = json.loads(message["properties"]["files"])
    files[0]["fileName"] = transcript_name
    message["properties"]["files"] = json.dumps(files)

    artifact = await services.exports.export(ExportRequest(conversation_ids=[ONE]))

    with zipfile.ZipFile(artifact.path) as archive:
        names = archive.namelist()
        transcript_path = next(name for name in names if name.endswith(f"/{transcript_name}"))
        asset_path = next(name for name in names if name.endswith(f"/{packaging.filename_stem(chat)}-2.txt"))
        assert archive.read(transcript_path).startswith(b"Conversation:")
        assert archive.read(asset_path) == b"attachment"


async def test_latest_n_and_dates_and_deleted_marker(services: Services, fake: FakeTeams) -> None:
    await services.exports.export(ExportRequest(conversation_ids=[ONE]))
    fake.chats[ONE].messages[2] = cs_message("1003", "2026-09-03T10:00:00.000Z", kind="MessageDelete",
                                             deleted="2026-09-04T00:00:00.000Z")  # fmt: skip
    artifact = await services.exports.export(
        ExportRequest(
            conversation_ids=[ONE],
            message_limit=2,
            until=datetime(2026, 9, 30, tzinfo=UTC),
            include_attachments=False,
        )
    )
    text = Path(artifact.path).read_text(encoding="utf-8")
    assert "First hello" not in text and artifact.message_count == 2
    assert (
        "[Soft deleted at 2026-09-04T00:00:00.000Z]" in text and "notes.txt" not in text
    )  # Teams keeps no text


async def test_several_chats_always_return_outer_zip_and_jsonl(services: Services) -> None:
    ids = [ONE, CHANNEL]
    zipped = await services.exports.export(ExportRequest(conversation_ids=ids))
    with zipfile.ZipFile(zipped.path) as archive:
        names = archive.namelist()
        assert len(names) == 2
        chat_archive_name = next(name for name in names if name.endswith(".zip"))
        assert any(name.endswith(".txt") for name in names)
        with zipfile.ZipFile(archive.open(chat_archive_name)) as chat_archive:
            chat_names = chat_archive.namelist()
            text_name = next(name for name in chat_names if name.endswith(".txt"))
            text_folder = text_name.rpartition("/")[0]
            assert text_folder and f"{text_folder}/notes.txt" in chat_names
    text_only = await services.exports.export(ExportRequest(conversation_ids=ids, include_attachments=False))
    with zipfile.ZipFile(text_only.path) as archive:
        names = archive.namelist()
        assert len(names) == 2 and all(name.endswith(".txt") for name in names)
    records = await services.exports.export(ExportRequest(conversation_ids=[CHANNEL], format="jsonl"))
    lines = [json.loads(line) for line in Path(records.path).read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in lines] == ["5003", "5001", "5002"] and lines[2]["reply_to_id"] == "5001"


async def test_download_attachment_reads_its_message_from_teams(services: Services) -> None:
    downloaded = await services.files.download_attachments(ONE, message_id="1003", sent_at=None, names=None)
    (saved,) = downloaded.files
    assert Path(saved.path).read_bytes() == b"shared notes" and saved.name == "notes.txt"
    with pytest.raises(NotFound):
        await services.files.download_attachments(ONE, message_id="1001", sent_at=None, names=None)


async def test_cancelled_export_during_fetch_publishes_no_file(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    export_dir = tmp_path / "exports"

    def kept(_: str) -> Path:
        export_dir.mkdir(parents=True, exist_ok=True)
        return export_dir

    monkeypatch.setattr(localfiles, "kept_dir", kept)

    async def wait_for_cancel(*_: object, **__: object) -> AsyncIterator[list[object]]:
        entered.set()
        await asyncio.Future()
        yield []  # never reached: the read waits until the export is cancelled

    monkeypatch.setattr(services.history, "selection", wait_for_cancel)
    task = asyncio.create_task(services.exports.export(ExportRequest(conversation_ids=[ONE])))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(export_dir.iterdir()) == []


def test_failed_zip_packaging_removes_the_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_paths = [tmp_path / "first.txt", tmp_path / "second.txt"]
    for path in source_paths:
        path.write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(packaging.localfiles, "kept_dir", lambda _: tmp_path)

    def fail_to_open_zip(*_: object, **__: object) -> None:
        raise OSError("synthetic archive failure")

    monkeypatch.setattr(packaging.zipfile, "ZipFile", fail_to_open_zip)
    with pytest.raises(OSError, match="synthetic archive failure"):
        packaging.package(source_paths, zip_name="synthetic.zip")
    assert not list(tmp_path.glob("synthetic*.zip"))
