"""Teams rich text → readable text, through both backends' message mappings."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from teams_connector.remote import chat_service_mapping, graph_mapping

MENTION = "<span itemtype='http://schema.skype.com/Mention' itemid='{i}'>{name}</span>"


def text(raw: dict[str, Any]) -> str:
    return chat_service_mapping.to_message({"id": "m", **raw}, "c").text


def html(content: str) -> str:
    return text({"messagetype": "RichText/Html", "content": content})


def test_rich_html_is_detected_from_the_message_type() -> None:
    assert text({"messagetype": "RichText/Html", "contenttype": "text",
                 "content": "<p>Hello <strong>world</strong></p>"}) == "Hello **world**"  # fmt: skip


@pytest.mark.parametrize("name", ["Example, User", "@Example, User"])
def test_mentions_are_delimited_without_a_duplicate_at(name: str) -> None:
    assert (
        html(f"<p>{MENTION.format(i=0, name=name)}, Could you respond?</p>")
        == "@(Example, User), Could you respond?"
    )


@pytest.mark.parametrize("separator", ["&nbsp;", " ", "\n\t", ""])
def test_split_mention_fragments_merge(separator: str) -> None:
    content = MENTION.format(i=0, name="Example,") + separator + MENTION.format(i=1, name="User")
    assert html(content) == "@(Example, User)"


def test_sequential_people_and_complete_mentions() -> None:
    content = (
        MENTION.format(i=0, name="Baxter,") + MENTION.format(i=1, name="Jonah") + " "
        + MENTION.format(i=2, name="Reyes,") + MENTION.format(i=3, name="Carla") + " "
        + MENTION.format(i=4, name="Parker, Juniper") + MENTION.format(i=5, name="Example, User")
    )  # fmt: skip
    assert html(content) == "@(Baxter, Jonah) @(Reyes, Carla) @(Parker, Juniper)@(Example, User)"
    connective = (
        MENTION.format(i=0, name="Baxter, Jonah") + " and " + MENTION.format(i=1, name="Reyes, Carla")
    )
    assert html(connective) == "@(Baxter, Jonah) and @(Reyes, Carla)"
    assert html(MENTION.format(i=0, name="Baxter,") + MENTION.format(i=1, name="Reyes, Carla")) == (
        "@(Baxter,)@(Reyes, Carla)"
    )
    assert (
        html(MENTION.format(i=0, name="Parker") + " " + MENTION.format(i=1, name="Juniper"))
        == "@(Parker) @(Juniper)"
    )


def test_lists_links_quotes_and_code_are_readable() -> None:
    rendered = html(
        "<p>See <a href='https://example.test'>docs</a></p><ul><li>first</li><li>second</li></ul>"
        "<blockquote>quoted</blockquote><pre><code>value = 1</code></pre>"
    )
    for part in ("docs (https://example.test)", "- first", "- second", "> quoted", "value = 1", "```"):
        assert part in rendered


def test_attachment_and_image_markers() -> None:
    message = chat_service_mapping.to_message(
        {
            "id": "m",
            "messageType": "RichText/Html",
            "content": "<p>Review</p><div itemtype='http://schema.skype.com/AMSImage'>"
            "<img src='https://images.example.test/one.png'></div>",
            "properties": {"files": json.dumps([{"fileName": "report.xlsx",
                                                 "fileInfo": {"shareUrl": "https://files.example.test/report"}}])},
        },
        "c",
    )  # fmt: skip
    assert "[Attachment: report.xlsx]" in message.text and "https://files.example.test/report" in message.text
    assert "[Image: https://images.example.test/one.png]" in message.text
    assert message.attachments[0].url == "https://files.example.test/report"


def test_swift_card_text_is_decoded_without_layout_or_payload() -> None:
    card = {
        "type": "message/card",
        "summary": "Approval required",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "body": [{"type": "Container", "spacing": "large", "items": [
                    {"type": "TextBlock", "text": "Approval required", "weight": "bolder"},
                    {"type": "TextBlock", "text": "Please review this request."},
                    {"type": "FactSet", "facts": [{"title": "Case ID", "value": "CASE-2048"}]},
                    {"type": "Image", "url": "https://images.example.test/layout.png", "altText": "Diagram"},
                ]}],
                "actions": [{"type": "Action.OpenUrl", "title": "Open case", "url": "https://example.test/c"}],
            },
        }],
        "entities": [{"type": "layout-only", "value": "discard this metadata"}],
    }  # fmt: skip
    encoded = base64.b64encode(json.dumps(card).encode()).decode()
    rendered = html(
        '<p>Before card</p><URIObject type="SWIFT.1" url_thumbnail="https://images.example.test/p.png">'
        '<a href="https://cards.example.test">Card preview</a><Title>Approval required</Title>'
        f'<Swift b64="{encoded}" /><Description /></URIObject><p>After card</p>'
    )
    for part in ("Before card", "After card", "Please review this request.", "Case ID: CASE-2048", "Diagram",
                 "Open case", "https://example.test/c", "https://cards.example.test"):  # fmt: skip
        assert part in rendered
    assert rendered.count("Approval required") == 1
    for absent in (encoded, "discard this metadata", "weight", "url_thumbnail"):
        assert absent not in rendered


def test_malformed_and_plain_swift_payloads_are_sanitized() -> None:
    rendered = html(
        '<URIObject type="SWIFT.1"><Title>Visible fallback</Title><Swift b64="not-valid-%" /></URIObject>'
    )
    assert "Visible fallback" in rendered and "not-valid" not in rendered and "b64" not in rendered

    card = {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "Decoded plain message"}]}
    encoded = base64.b64encode(json.dumps(card).encode()).decode()
    content = f'<URIObject type="SWIFT.1"><Title>Visible plain</Title><Swift b64="{encoded}" /></URIObject>'
    plain = text({"messageType": "Text", "content": content})
    assert "Visible plain" in plain
    assert "Decoded plain message" in plain
    assert encoded not in plain


def test_chat_service_record_fields() -> None:
    message = chat_service_mapping.to_message(
        {
            "id": "42",
            "originalarrivaltime": "2026-09-01T10:00:00.123Z",
            "messagetype": "RichText/Html",
            "content": "<p>Hi</p>",
            "imdisplayname": "Pat Peer",
            "from": "https://x.teams.microsoft.com/v1/users/ME/contacts/8:orgid:33333333-3333-3333-3333-333333333333",
            "properties": {"edittime": "1790000000000"},
        },
        "c",
    )
    assert message.author_id == "33333333-3333-3333-3333-333333333333" and message.author == "Pat Peer"
    assert message.sent_at and message.sent_at.microsecond == 123000 and message.edited_at
    deleted = chat_service_mapping.to_message({"id": "1", "messagetype": "MessageDelete"}, "c")
    assert deleted.is_deleted and deleted.system_event is None and deleted.text == ""
    event = chat_service_mapping.to_message({"id": "2", "messagetype": "ThreadActivity/AddMember"}, "c")
    assert (
        event.system_event == "ThreadActivity/AddMember"
        and event.text == "[System message: ThreadActivity/AddMember]"
    )


def test_graph_message_mapping() -> None:
    raw = {
        "id": "7",
        "replyToId": "6",
        "messageType": "message",
        "createdDateTime": "2026-09-01T10:00:00Z",
        "lastEditedDateTime": "2026-09-01T11:00:00Z",
        "subject": "Plan",
        "from": {"user": {"id": "AAAAAAAA-0000-0000-0000-000000000001", "displayName": "Ann"}},
        "body": {
            "contentType": "html",
            "content": ('<p><at id="0">Example, User</at> see <attachment id="a1"></attachment></p>'),
        },
        "attachments": [
            {
                "id": "a1",
                "contentType": "reference",
                "name": "deck.pptx",
                "contentUrl": "https://t.sharepoint.com/deck",
            },
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": json.dumps(
                    {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "Poll"}]}
                ),
            },
        ],
    }
    message = graph_mapping.to_message(raw, "19:c@thread.tacv2")
    assert message.text.startswith("@(Example, User) see")
    assert "Poll" in message.text and "[Attachment: deck.pptx]" in message.text
    assert message.author_id == "aaaaaaaa-0000-0000-0000-000000000001" and message.reply_to_id == "6"
    assert message.subject == "Plan" and message.edited_at and message.attachments[0].url

    bot_raw = {
        "id": "8",
        "createdDateTime": "2026-09-01T10:00:00Z",
        "from": {"application": {"id": "APP", "displayName": "Bot"}},
        "body": {"contentType": "text", "content": "beep"},
    }
    bot = graph_mapping.to_message(bot_raw, "c")
    assert bot.author_id == "28:app" and bot.text == "beep"

    event_raw = {
        "id": "9",
        "messageType": "systemEventMessage",
        "createdDateTime": "2026-09-01T10:00:00Z",
        "eventDetail": {"@odata.type": "#microsoft.graph.membersAddedEventMessageDetail"},
        "body": {"contentType": "html", "content": "<systemEventMessage/>"},
    }
    event = graph_mapping.to_message(event_raw, "c")
    assert event.system_event == "membersAdded" and event.text == "[System message: membersAdded]"
