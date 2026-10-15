"""Sending files: validated locally, uploaded and shared once each, posted only when all of that worked.

Synthetic fixtures and the fake tenant only.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from teams_connector import config
from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest, Upstream, WriteOutcomeUnknown
from teams_connector.remote import graph_teams
from teams_connector.remote.graph import Graph
from teams_connector.remote.graph_teams import GraphFileSharer
from teams_connector.remote.transport import Bearer, Transport
from teams_connector.service import send
from tests.fakes.teams_fake import PEER, THIRD, FakeTeams, FakeTokens

ONE = "19:one@unq.gbl.spaces"
GROUP = "19:group@thread.v2"


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    path = tmp_path / "notes.txt"
    path.write_bytes(b"synthetic notes")
    return path


@pytest.fixture
def plan(tmp_path: Path) -> Path:
    path = tmp_path / "plan.pdf"
    path.write_bytes(b"%PDF synthetic plan")
    return path


async def _name(services: Services, conversation_id: str) -> str:
    await services.catalog.refresh()
    chat = services.store.chat(conversation_id)
    assert chat is not None
    return chat.name


async def test_a_file_is_uploaded_shared_with_the_peer_and_referenced_by_the_message(
    services: Services, fake: FakeTeams, notes: Path
) -> None:
    result = await services.sender.send(
        ONE, expected_chat_name="Pat Peer", text="Here are the notes.", files=[str(notes)]
    )

    assert fake.uploads == [(config.CHAT_FILES_FOLDER, "notes.txt", b"synthetic notes")]
    [(item, invite, prefer)] = fake.invites
    assert item == "ITEM1"
    assert invite == {
        "recipients": [{"objectId": PEER}],
        "roles": ["write"],
        "requireSignIn": True,
        "sendInvitation": False,
    }
    assert prefer == "getDefaultLink"
    _, body = fake.sent[-1]
    guid = "00000001-aaaa-bbbb-cccc-ddddeeeeffff"
    assert body["body"]["content"] == f'<p>Here are the notes.</p><attachment id="{guid}"></attachment>'
    assert body["attachments"] == [
        {
            "id": guid,
            "contentType": "reference",
            "contentUrl": "https://tenant-my.sharepoint.com/personal/mia/Documents/chat/notes.txt",
            "name": "notes.txt",
        }
    ]
    assert [f.name for f in result.files] == ["notes.txt"]


async def test_a_group_shares_every_file_with_every_other_member_in_one_invite(
    services: Services, fake: FakeTeams, notes: Path, plan: Path
) -> None:
    name = await _name(services, GROUP)

    await services.sender.send(GROUP, expected_chat_name=name, files=[str(notes), str(plan)])

    assert [stored for _, stored, _ in fake.uploads] == ["notes.txt", "plan.pdf"]
    assert [item for item, _, _ in fake.invites] == ["ITEM1", "ITEM2"]
    for _, invite, _ in fake.invites:
        assert sorted(r["objectId"] for r in invite["recipients"]) == sorted([PEER, THIRD])
    assert len(fake.sent) == 1 and len(fake.sent[0][1]["attachments"]) == 2


async def test_files_without_text_send_only_the_attachments(
    services: Services, fake: FakeTeams, notes: Path
) -> None:
    result = await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(notes)])

    content = fake.sent[-1][1]["body"]["content"]
    assert content.startswith("<attachment id=") and "<p>" not in content
    assert result.characters == 0


async def test_a_name_clash_is_renamed_and_reported(services: Services, fake: FakeTeams, notes: Path) -> None:
    await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(notes)])
    result = await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(notes)])

    assert [f.name for f in result.files] == ["notes 1.txt"]


async def test_direct_access_grants_without_asking_for_the_default_link(fake: FakeTeams, notes: Path) -> None:
    async with httpx.AsyncClient(transport=fake.transport()) as client:
        graph = Graph(Transport(client=client), Bearer(FakeTokens(), "teams.graph"))  # type: ignore[arg-type]
        sharer = GraphFileSharer(graph, folder="Synthetic Folder", access="direct")
        file = await sharer.upload(notes)
        await sharer.share(file, [PEER])

    assert fake.uploads[-1][0] == "Synthetic Folder"
    assert fake.invites[-1][2] != "getDefaultLink"


@pytest.mark.parametrize(
    ("conversation_id", "message"),
    [
        ("19:general@thread.tacv2", "one-on-one and group chats and the self chat only"),
        ("19:meeting_abc@thread.v2", "one-on-one and group chats and the self chat only"),
    ],
)
async def test_files_are_refused_in_channels_and_meeting_chats(
    services: Services, fake: FakeTeams, notes: Path, conversation_id: str, message: str
) -> None:
    name = await _name(services, conversation_id)

    with pytest.raises(InvalidRequest, match=message):
        await services.sender.send(conversation_id, expected_chat_name=name, files=[str(notes)])
    assert fake.uploads == [] and fake.sent == []


async def test_the_self_chat_takes_files_without_a_share_step(
    services: Services, fake: FakeTeams, notes: Path
) -> None:
    name = await _name(services, "48:notes")

    result = await services.sender.send("48:notes", expected_chat_name=name, text="Mine.", files=[str(notes)])

    assert [stored for _, stored, _ in fake.uploads] == ["notes.txt"]
    assert fake.invites == []
    path, body = fake.sent[-1]
    assert path == "/chats/48:notes/messages" and len(body["attachments"]) == 1
    assert [f.name for f in result.files] == ["notes.txt"]


async def test_bad_paths_are_refused_before_any_request(
    services: Services, fake: FakeTeams, notes: Path, tmp_path: Path
) -> None:
    for files, message in [
        (["notes.txt"], "absolute"),
        ([str(tmp_path / "missing.txt")], "Not a readable file"),
        ([str(tmp_path)], "Not a readable file"),
        ([str(notes)] * (send.MAX_FILES + 1), "at most"),
    ]:
        with pytest.raises(InvalidRequest, match=message):
            await services.sender.send(ONE, expected_chat_name="Pat Peer", files=files)
    assert fake.calls == []


async def test_a_file_over_the_configured_cap_is_refused(
    services: Services, fake: FakeTeams, notes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "MAX_SEND_FILE_BYTES", 4 * 1024**2)
    notes.write_bytes(b"x" * (4 * 1024**2 + 1))

    with pytest.raises(InvalidRequest, match=r"the limit is 4 MiB \(config.MAX_SEND_FILE_BYTES\)"):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(notes)])
    assert fake.calls == []


def test_the_default_cap_is_two_gib() -> None:
    assert config.MAX_SEND_FILE_BYTES == 2 * 1024**3


@pytest.fixture
def large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A 25-byte file that counts as large: upload sessions start at 16 bytes, in 10-byte chunks."""
    monkeypatch.setattr(graph_teams, "SINGLE_UPLOAD_BYTES", 16)
    monkeypatch.setattr(graph_teams, "UPLOAD_CHUNK_BYTES", 10)
    path = tmp_path / "video.mp4"
    path.write_bytes(bytes(range(65, 90)))
    return path


def _chunk_puts(fake: FakeTeams) -> int:
    return sum(call.startswith("PUT tenant-my.sharepoint.com") for call in fake.calls)


async def test_a_large_file_goes_up_in_chunks_without_credentials(
    services: Services, fake: FakeTeams, large: Path
) -> None:
    result = await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(large)])

    assert fake.uploads == [(config.CHAT_FILES_FOLDER, "video.mp4", large.read_bytes())]
    assert _chunk_puts(fake) == 3
    assert fake.upload_session_auth and set(fake.upload_session_auth) == {None}
    assert not any(c.startswith("PUT graph.microsoft.com") for c in fake.calls)  # no single PUT
    assert [f.name for f in result.files] == ["video.mp4"] and len(fake.invites) == 1
    assert len(fake.sent[-1][1]["attachments"]) == 1


async def test_a_file_below_the_session_threshold_is_one_put(
    services: Services, fake: FakeTeams, large: Path, notes: Path
) -> None:
    await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(notes)])  # 15 bytes

    assert fake.upload_sessions == {} and len(fake.uploads) == 1


@pytest.mark.parametrize(
    ("failure", "puts"),
    [
        ("500", 4),  # nothing kept: the chunk is sent again
        ("lost", 3),  # kept but unanswered: the status says it arrived, so the next chunk follows
    ],
)
async def test_a_failed_chunk_resends_only_the_missing_ranges(
    services: Services, fake: FakeTeams, large: Path, failure: str, puts: int
) -> None:
    fake.chunk_failures = [(10, failure)]

    await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(large)])

    assert fake.uploads == [(config.CHAT_FILES_FOLDER, "video.mp4", large.read_bytes())]
    assert _chunk_puts(fake) == puts
    assert sum(call.startswith("GET tenant-my.sharepoint.com") for call in fake.calls) == 1
    assert fake.cancelled_uploads == [] and len(fake.sent) == 1


async def test_a_bounded_missing_range_is_sent_without_overlapping_received_bytes(
    services: Services, fake: FakeTeams, large: Path
) -> None:
    data = large.read_bytes()
    fake.upload_prefill = {offset: data[offset] for offset in (12, 13, 14)}  # Graph names "10-11" and "15-"

    await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(large)])

    assert fake.uploads == [(config.CHAT_FILES_FOLDER, "video.mp4", data)]
    assert _chunk_puts(fake) == 3  # 0-9, then only 10-11, then 15-24


async def test_repeated_chunk_failures_cancel_the_session_and_post_nothing(
    services: Services, fake: FakeTeams, large: Path
) -> None:
    fake.chunk_failures = [(10, "500")] * (graph_teams.UPLOAD_RESUMES + 1)

    with pytest.raises(Upstream):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(large)])

    assert _chunk_puts(fake) == 1 + graph_teams.UPLOAD_RESUMES + 1
    assert fake.cancelled_uploads == ["1"]
    assert fake.uploads == [] and fake.invites == [] and fake.sent == []


async def test_an_unreadable_status_after_the_last_chunk_is_an_unknown_outcome(
    services: Services, fake: FakeTeams, large: Path
) -> None:
    fake.chunk_failures = [(20, "lost")]
    fake.fail_upload_status = True

    with pytest.raises(WriteOutcomeUnknown, match="video.mp4 may also have been uploaded"):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(large)])

    assert len(fake.uploads) == 1 and fake.sent == []  # the fake completed it; nothing was posted


async def test_an_unreadable_status_mid_file_cancels_and_reports_the_chunk_failure(
    services: Services, fake: FakeTeams, large: Path
) -> None:
    fake.chunk_failures = [(10, "500")]
    fake.fail_upload_status = True

    with pytest.raises(Upstream, match="generalException"):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(large)])

    assert fake.cancelled_uploads == ["1"] and fake.uploads == [] and fake.sent == []


async def test_a_failed_upload_posts_nothing_and_names_the_uploaded_files(
    services: Services, fake: FakeTeams, notes: Path, plan: Path
) -> None:
    fake.fail_upload_names = {"plan.pdf"}

    with pytest.raises(Upstream) as raised:
        await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(notes), str(plan)])

    assert "Uploading plan.pdf failed" in str(raised.value) and "notes.txt (https://" in str(raised.value)
    assert fake.invites == [] and fake.sent == []


async def test_a_failed_share_posts_nothing(services: Services, fake: FakeTeams, notes: Path) -> None:
    fake.fail_invites = True

    with pytest.raises(Upstream, match="Sharing notes.txt failed"):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", files=[str(notes)])
    assert fake.sent == []


async def test_a_partly_failed_share_posts_nothing(services: Services, fake: FakeTeams, notes: Path) -> None:
    fake.partial_invites = True
    name = await _name(services, GROUP)

    with pytest.raises(Upstream, match=r"Sharing notes.txt failed.*1 of 2 chat members \(notAllowed\)"):
        await services.sender.send(GROUP, expected_chat_name=name, files=[str(notes)])
    assert fake.sent == []


async def test_a_member_without_an_id_refuses_the_send_before_any_upload(
    services: Services, fake: FakeTeams, notes: Path
) -> None:
    name = await _name(services, GROUP)
    fake.chats[GROUP].members.append(("", None))  # Chat Service lists a member with no usable id

    with pytest.raises(InvalidRequest, match="outside your organization's directory"):
        await services.sender.send(GROUP, expected_chat_name=name, files=[str(notes)])
    assert fake.uploads == [] and fake.sent == []


async def test_an_unconfirmed_post_reports_the_shared_files_and_is_not_retried(
    services: Services, fake: FakeTeams, notes: Path
) -> None:
    fake.fail_sends = "504"

    with pytest.raises(WriteOutcomeUnknown, match="uploaded and shared") as raised:
        await services.sender.send(ONE, expected_chat_name="Pat Peer", text="Notes.", files=[str(notes)])

    assert "notes.txt" in str(raised.value)
    assert len(fake.uploads) == 1 and len(fake.invites) == 1
    posts = [call for call in fake.calls if call.startswith("POST graph.microsoft.com/v1.0/chats/")]
    assert len(posts) == 1
