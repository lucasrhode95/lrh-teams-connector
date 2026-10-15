from __future__ import annotations

from teams_connector.remote import chat_service_mapping, graph_mapping


def _text(content: str) -> str:
    raw = {"id": "1", "messagetype": "RichText/Html", "content": content}
    return chat_service_mapping.to_message(raw, "c").text


def test_consecutive_paragraphs_read_as_consecutive_lines() -> None:
    assert _text("<p>first line</p><p>second line</p>") == "first line\nsecond line"


def test_line_breaks_inside_a_paragraph_stay_single_breaks() -> None:
    assert _text("<p>first<br>second</p>") == "first\nsecond"


def test_an_empty_paragraph_keeps_its_blank_line() -> None:
    assert _text("<p>a</p><p><br></p><p>b</p>") == "a\n\nb"
    assert _text("<p>a</p><p></p><p>b</p>") == "a\n\nb"


def test_line_breaks_between_paragraphs_are_layout_not_blank_lines() -> None:
    assert _text("<p>first line</p>\r\n<p>second line</p>") == "first line\nsecond line"


def test_code_blocks_keep_their_line_breaks() -> None:
    assert "a\nb" in _text("<pre><code>a\r\nb</code></pre>")


def test_graph_messages_use_the_same_paragraph_rule() -> None:
    raw = {
        "id": "2",
        "createdDateTime": "2026-09-01T10:00:00Z",
        "from": {"user": {"id": "AAAAAAAA-0000-0000-0000-000000000001", "displayName": "Ann"}},
        "body": {"contentType": "html", "content": "<p>first line</p>\r\n<p>second line</p>"},
    }
    assert graph_mapping.to_message(raw, "c").text == "first line\nsecond line"
