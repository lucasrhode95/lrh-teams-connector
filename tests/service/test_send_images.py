"""Sending inline images: PNG and JPEG only, named by file name in the HTML, checked before any request.

Synthetic fixtures and the fake tenant only.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest
from teams_connector.service import send
from tests.fakes.teams_fake import FakeTeams

ONE = "19:one@unq.gbl.spaces"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-chart"
JPEG = b"\xff\xd8\xff\xe0synthetic-photo"


@pytest.fixture
def chart(tmp_path: Path) -> Path:
    path = tmp_path / "chart.png"
    path.write_bytes(PNG)
    return path


@pytest.fixture
def photo(tmp_path: Path) -> Path:
    path = tmp_path / "photo.jpg"
    path.write_bytes(JPEG)
    return path


def _hosted(number: int, data: bytes, kind: str) -> dict[str, str]:
    return {
        "@microsoft.graph.temporaryId": str(number),
        "contentBytes": base64.b64encode(data).decode(),
        "contentType": kind,
    }


async def test_an_image_named_in_html_is_sent_as_hosted_content(
    services: Services, fake: FakeTeams, chart: Path
) -> None:
    html = '<p>Results:</p>\n<p><img alt="Q3 &amp; Q4" src="chart.png" width="300"></p>'

    result = await services.sender.send(ONE, expected_chat_name="Pat Peer", html=html, images=[str(chart)])

    body = fake.sent[-1][1]
    assert body["body"]["content"] == (
        '<p>Results:</p>\n<p><img alt="Q3 &amp; Q4" src="../hostedContents/1/$value" width="300"></p>'
    )
    assert body["hostedContents"] == [_hosted(1, PNG, "image/png")]
    assert result.characters == len(html)


async def test_with_text_the_images_follow_it(
    services: Services, fake: FakeTeams, chart: Path, photo: Path
) -> None:
    await services.sender.send(
        ONE, expected_chat_name="Pat Peer", text="Two pictures.", images=[str(chart), str(photo)]
    )

    body = fake.sent[-1][1]
    assert body["body"]["content"] == (
        '<p>Two pictures.</p><p><img src="../hostedContents/1/$value"></p>'
        '<p><img src="../hostedContents/2/$value"></p>'
    )
    assert body["hostedContents"] == [_hosted(1, PNG, "image/png"), _hosted(2, JPEG, "image/jpeg")]


async def test_images_the_html_does_not_name_follow_it(
    services: Services, fake: FakeTeams, chart: Path, photo: Path
) -> None:
    html = '<p><img src="photo.jpg"> and the chart below</p>'

    await services.sender.send(ONE, expected_chat_name="Pat Peer", html=html, images=[str(chart), str(photo)])

    assert fake.sent[-1][1]["body"]["content"] == (
        '<p><img src="../hostedContents/2/$value"> and the chart below</p>'
        '<p><img src="../hostedContents/1/$value"></p>'
    )


async def test_images_alone_are_a_message(services: Services, fake: FakeTeams, chart: Path) -> None:
    await services.sender.send(ONE, expected_chat_name="Pat Peer", images=[str(chart)])

    assert fake.sent[-1][1]["body"]["content"] == '<p><img src="../hostedContents/1/$value"></p>'


async def test_images_and_files_share_a_message(
    services: Services, fake: FakeTeams, chart: Path, tmp_path: Path
) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"synthetic notes")

    await services.sender.send(
        ONE, expected_chat_name="Pat Peer", text="Both.", files=[str(notes)], images=[str(chart)]
    )

    body = fake.sent[-1][1]
    assert len(body["hostedContents"]) == 1 and len(body["attachments"]) == 1
    assert body["body"]["content"].startswith('<p>Both.</p><p><img src="../hostedContents/1/$value"></p>')
    assert "<attachment id=" in body["body"]["content"]


async def test_the_self_chat_takes_images(services: Services, fake: FakeTeams, chart: Path) -> None:
    await services.catalog.refresh()
    chat = services.store.chat("48:notes")
    assert chat is not None

    await services.sender.send("48:notes", expected_chat_name=chat.name, images=[str(chart)])

    path, body = fake.sent[-1]
    assert path == "/chats/48:notes/messages" and len(body["hostedContents"]) == 1


@pytest.mark.parametrize(
    ("html", "message"),
    [
        ('<img src="https://example.com/remote.png">', "Embedded media"),
        ('<img src="other.png">', "Embedded media"),
        ('<img src="chart.png" srcset="https://example.com/remote.png 2x">', "only alt, title"),
        ('<img src="chart.png" onerror="alert(1)">', "only alt, title"),
        ('<img src="chart.png" style="x" title="javascript:alert(1)">', "Active HTML"),
    ],
)
async def test_an_img_that_names_no_image_is_refused(
    services: Services, fake: FakeTeams, chart: Path, html: str, message: str
) -> None:
    with pytest.raises(InvalidRequest, match=message):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", html=html, images=[str(chart)])
    assert fake.calls == []


async def test_an_image_that_is_not_png_or_jpeg_is_refused_before_any_request(
    services: Services, fake: FakeTeams, tmp_path: Path
) -> None:
    animation = tmp_path / "animation.png"  # the name says PNG, the content is a GIF
    animation.write_bytes(b"GIF89a synthetic")

    with pytest.raises(InvalidRequest, match="animation.png is not a PNG or JPEG.*files instead"):
        await services.sender.send(ONE, expected_chat_name="Pat Peer", images=[str(animation)])
    assert fake.calls == []


async def test_images_too_large_for_one_request_are_refused_before_any_request(
    services: Services, fake: FakeTeams, chart: Path, photo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(send, "MAX_IMAGE_REQUEST_BYTES", 2 * send.IMAGE_JSON_OVERHEAD + 40)

    with pytest.raises(InvalidRequest, match="about 3 MB of images in total. Send them in files instead"):
        await services.sender.send(
            ONE, expected_chat_name="Pat Peer", text="Hi", images=[str(chart), str(photo)]
        )
    assert fake.calls == []


async def test_two_images_with_one_name_are_refused(
    services: Services, fake: FakeTeams, chart: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "chart.png").write_bytes(PNG)

    with pytest.raises(InvalidRequest, match="Two images are named chart.png"):
        await services.sender.send(
            ONE, expected_chat_name="Pat Peer", images=[str(chart), str(other / "chart.png")]
        )
    assert fake.calls == []
