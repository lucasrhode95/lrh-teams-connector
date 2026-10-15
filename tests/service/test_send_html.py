"""Sending HTML: exactly one of text or html; HTML is checked, measured and sent as written.

Synthetic fixtures only.
"""

from __future__ import annotations

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest
from tests.fakes.teams_fake import FakeTeams

ONE = "19:one@unq.gbl.spaces"
CONFIRMATION = "Synthetic authorization for a service test."
HTML = (
    "<p><strong>Due Friday</strong></p><ul><li>review the <code>budget</code> sheet</li>"
    '<li><a href="https://example.com/tracker">tracker</a></li></ul>'
)


async def test_html_is_sent_as_written(services: Services, fake: FakeTeams) -> None:
    result = await services.sender.send(
        ONE, expected_chat_name="Pat Peer", html=HTML, user_confirmation=CONFIRMATION
    )

    path, body = fake.sent[-1]
    assert body == {"body": {"contentType": "html", "content": HTML}}
    assert result.characters == len(HTML)
    assert path.endswith("/messages")


async def test_plain_text_is_escaped_and_still_sent_as_paragraphs(
    services: Services, fake: FakeTeams
) -> None:
    await services.sender.send(
        ONE, expected_chat_name="Pat Peer", text="<b>x</b> & y", user_confirmation=CONFIRMATION
    )

    assert fake.sent[-1][1]["body"]["content"] == "<p>&lt;b&gt;x&lt;/b&gt; &amp; y</p>"


@pytest.mark.parametrize("kwargs", [{}, {"text": "a", "html": "<p>a</p>"}])
async def test_exactly_one_of_text_or_html_is_required(
    services: Services, fake: FakeTeams, kwargs: dict[str, str]
) -> None:
    with pytest.raises(InvalidRequest, match="exactly one"):
        await services.sender.send(
            ONE, expected_chat_name="Pat Peer", user_confirmation=CONFIRMATION, **kwargs
        )
    assert fake.sent == []


async def test_active_html_is_refused_before_anything_is_sent(services: Services, fake: FakeTeams) -> None:
    with pytest.raises(InvalidRequest, match="script"):
        await services.sender.send(
            ONE,
            expected_chat_name="Pat Peer",
            html="<p>ok</p><script>alert(1)</script>",
            user_confirmation=CONFIRMATION,
        )
    assert fake.sent == []


async def test_html_is_limited_to_ten_thousand_characters(services: Services, fake: FakeTeams) -> None:
    with pytest.raises(InvalidRequest, match="10,000"):
        await services.sender.send(
            ONE,
            expected_chat_name="Pat Peer",
            html="<p>" + "a" * 10_000 + "</p>",
            user_confirmation=CONFIRMATION,
        )
    assert fake.sent == []
