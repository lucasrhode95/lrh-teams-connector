from __future__ import annotations

import asyncio
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest, NotFound, WriteOutcomeUnknown
from teams_connector.domain.models import ExportProgress, ExportRequest
from teams_connector.service import localfiles
from teams_connector.service.export import packaging
from tests.fakes.teams_fake import FakeTeams, cs_message

ONE = "19:one@unq.gbl.spaces"
CHANNEL = "19:general@thread.tacv2"


async def test_send_checks_the_live_name_and_posts_escaped_html(services: Services, fake: FakeTeams) -> None:
    result = await services.sender.send(
        ONE, expected_chat_name=" pat  PEER ", text="a < b\nline 2", user_confirmation="user said: send it"
    )
    path, body = fake.sent[0]
    assert path == "/chats/19:one@unq.gbl.spaces/messages"
    assert body == {"body": {"contentType": "html", "content": "<p>a &lt; b<br>line 2</p>"}}
    assert result.message_id and result.sent_at and result.chat_name == "Pat Peer" and result.characters == 12


async def test_channel_send_posts_to_the_team_channel(services: Services, fake: FakeTeams) -> None:
    await services.sender.send(
        CHANNEL, expected_chat_name="Platform · General", text="hi", user_confirmation="yes, post it"
    )
    assert fake.sent[0][0] == "/teams/team-1/channels/19:general@thread.tacv2/messages"


async def test_send_refusals_never_reach_teams(services: Services, fake: FakeTeams) -> None:
    with pytest.raises(InvalidRequest, match="does not match"):
        await services.sender.send(ONE, expected_chat_name="Someone else", text="hi", user_confirmation="ok")
    with pytest.raises(InvalidRequest, match="user_confirmation"):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", text="hi", user_confirmation=" ")
    with pytest.raises(InvalidRequest, match="10,000"):
        await services.sender.send(
            ONE, expected_chat_name="Pat Peer", text="x" * 10_001, user_confirmation="ok"
        )
    with pytest.raises(NotFound):
        await services.sender.send(
            "19:nope@thread.v2", expected_chat_name="x", text="hi", user_confirmation="ok"
        )
    assert fake.sent == []


@pytest.mark.parametrize("failure", ["network", "504"])
async def test_ambiguous_send_is_reported_and_not_retried(
    services: Services, fake: FakeTeams, failure: str
) -> None:
    fake.fail_sends = failure
    with pytest.raises(WriteOutcomeUnknown):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", text="hi", user_confirmation="ok")
    assert sum(c.startswith("POST graph.microsoft.com/v1.0/chats") for c in fake.calls) == 1


async def test_single_chat_export_is_one_txt(services: Services) -> None:
    progress: list[ExportProgress] = []
    artifact = await services.exports.export(ExportRequest(conversation_ids=[ONE]), progress=progress.append)
    text = Path(artifact.path).read_text(encoding="utf-8")
    assert artifact.filename.endswith(".txt") and artifact.message_count == 3
    assert "Conversation: Pat Peer" in text and "Time zone: UTC" in text
    assert "[2026-09-02T10:00:00.000Z] Mia Example\nSecond, about the budget review" in text
    assert text.index("First hello") < text.index("Third")
    assert progress[-1].phase == "done" and progress[0].phase == "retrieving"


async def test_latest_n_and_dates_and_deleted_marker(services: Services, fake: FakeTeams) -> None:
    await services.exports.export(ExportRequest(conversation_ids=[ONE]))
    fake.chats[ONE].messages[2] = cs_message("1003", "2026-09-03T10:00:00.000Z", kind="MessageDelete",
                                             deleted="2026-09-04T00:00:00.000Z")  # fmt: skip
    artifact = await services.exports.export(
        ExportRequest(conversation_ids=[ONE], message_limit=2, until=datetime(2026, 9, 30, tzinfo=UTC))
    )
    text = Path(artifact.path).read_text(encoding="utf-8")
    assert "First hello" not in text and artifact.message_count == 2
    assert "[Soft deleted at 2026-09-04T00:00:00.000Z]" in text and "notes.txt" in text  # retained text


async def test_several_chats_zip_or_combine_and_jsonl(services: Services) -> None:
    ids = [ONE, CHANNEL]
    zipped = await services.exports.export(ExportRequest(conversation_ids=ids))
    with zipfile.ZipFile(zipped.path) as archive:
        assert len(archive.namelist()) == 2
    combined = await services.exports.export(ExportRequest(conversation_ids=ids, combine=True))
    text = Path(combined.path).read_text(encoding="utf-8")
    assert text.count("Conversation ID:") == 2 and "(reply)" in text and "Subject: Launch" in text
    records = await services.exports.export(ExportRequest(conversation_ids=[CHANNEL], format="jsonl"))
    lines = [json.loads(line) for line in Path(records.path).read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in lines] == ["5003", "5001", "5002"] and lines[2]["reply_to_id"] == "5001"


async def test_download_attachment_of_an_archived_message(services: Services) -> None:
    await services.history.get_messages(ONE)
    saved = await services.files.download_attachment(ONE, "1003")
    assert Path(saved.path).read_bytes() == b"shared notes" and saved.name == "notes.txt"
    with pytest.raises(NotFound):
        await services.files.download_attachment(ONE, "1001")


async def test_cancelled_export_during_fetch_publishes_no_file(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    export_dir = tmp_path / "exports"
    monkeypatch.setattr(localfiles, "kept_dir", lambda _: export_dir)

    async def wait_for_cancel(*_: object, **__: object) -> None:
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(services.history, "fetch_range", wait_for_cancel)
    task = asyncio.create_task(services.exports.export(ExportRequest(conversation_ids=[ONE])))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not export_dir.exists()


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
