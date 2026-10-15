"""Inline images: the marker each one shows, the attachment that can fetch it, and what is only text.

Synthetic fixtures only: the object ids, hosts and bytes are invented.
"""

from __future__ import annotations

import base64
import hashlib

from teams_connector.remote import chat_service_mapping, graph_mapping, inline_images

OBJECT = "0-neu-d1-0123456789abcdef0123456789abcdef"
AMS_SRC = f"https://eu-api.asm.skype.com/v1/objects/{OBJECT}/views/imgo"
FULL_SIZE = f"https://eu-api.asm.skype.com/v1/objects/{OBJECT}/views/imgpsh_fullsize"
AMS_TYPE = 'itemtype="http://schema.skype.com/AMSImage"'
CONVERSATION = "19:one@unq.gbl.spaces"


def chat_message(content: str):  # type: ignore[no-untyped-def]
    return chat_service_mapping.to_message(
        {"id": "1001", "messageType": "RichText/Html", "content": content}, CONVERSATION
    )


def test_ams_image_is_named_by_object_id_and_its_file_type() -> None:
    message = chat_message(f'<p>Look</p><img src="{AMS_SRC}" {AMS_TYPE} itemscope="jpeg" itemid="{OBJECT}">')
    assert f"[Image: {OBJECT}.jpeg]" in message.text
    (image,) = message.attachments
    assert (image.kind, image.source, image.source_id, image.url) == ("image", "ams", OBJECT, FULL_SIZE)
    assert image.name == f"{OBJECT}.jpeg"


def test_ams_image_without_a_file_type_is_named_png() -> None:
    message = chat_message(f'<img src="{AMS_SRC}" {AMS_TYPE} itemid="{OBJECT}">')
    assert f"[Image: {OBJECT}.png]" in message.text
    assert message.attachments[0].url == FULL_SIZE


def test_equal_names_within_one_message_get_a_number() -> None:
    tag = f'<img src="{AMS_SRC}" {AMS_TYPE} itemscope="png" itemid="{OBJECT}">'
    message = chat_message(f"<p>{tag}{tag}</p>")
    assert f"[Image: {OBJECT}.png]" in message.text and f"[Image: {OBJECT} (2).png]" in message.text
    assert [a.name for a in message.attachments] == [f"{OBJECT}.png", f"{OBJECT} (2).png"]


def test_emoji_and_stickers_are_their_text_and_never_base64() -> None:
    message = chat_message(
        '<p>Nice <img itemtype="http://schema.skype.com/Emoji" src="data:image/png;base64,AAAA" '
        'alt="\U0001f600" title=":grin:"> done</p>'
    )
    assert "\U0001f600" in message.text and "base64" not in message.text and "data:" not in message.text
    assert message.attachments == []


def test_gif_is_a_link_and_never_fetched() -> None:
    gif = "https://media1.giphy.com/media/synthetic/giphy.gif"
    message = chat_message(
        f'<img src="{gif}" itemtype="http://schema.skype.com/Giphy" alt="Dancing cat" width="200">'
    )
    assert f"[GIF: Dancing cat] {gif}" in message.text
    assert message.attachments == []


def test_data_image_is_named_by_its_digest_and_keeps_its_bytes_out_of_the_record() -> None:
    picture = b"\x89PNG\r\n\x1a\nsynthetic"
    src = "data:image/png;base64," + base64.b64encode(picture).decode()
    message = chat_message(f'<img src="{src}" alt="image">')
    digest = hashlib.sha256(picture).hexdigest()
    name = f"sha-{digest[:8]}.png"
    assert f"[Image: {name}]" in message.text and "base64" not in message.text
    (image,) = message.attachments
    assert (image.source, image.source_id, image.size, image.payload) == (
        "data",
        digest,
        len(picture),
        picture,
    )
    assert "payload" not in image.model_dump(mode="json")


def test_other_external_images_are_shown_as_links_and_not_fetched() -> None:
    message = chat_message('<img src="https://images.example.test/one.png" alt="one">')
    assert "[Image: https://images.example.test/one.png]" in message.text
    assert message.attachments == []


def test_an_ams_image_on_another_host_keeps_its_name_but_cannot_be_fetched() -> None:
    message = chat_message(f'<img src="https://elsewhere.example.test/x.png" {AMS_TYPE} itemid="{OBJECT}">')
    assert f"[Image: {OBJECT}.png]" in message.text
    assert (message.attachments[0].source, message.attachments[0].url) == (None, None)


def test_graph_hosted_copy_takes_the_ams_name_and_is_fetched_from_graph() -> None:
    src = "https://graph.microsoft.com/v1.0/teams/t1/channels/c1/messages/g1/hostedContents/h1/$value"
    raw = {
        "id": "g1",
        "body": {
            "contentType": "html",
            "content": f'<p>Shot</p><img src="{src}" itemid="{OBJECT}" alt="image" width="10">',
        },
    }
    message = graph_mapping.to_message(raw, "19:general@thread.tacv2")
    assert f"[Image: {OBJECT}.png]" in message.text
    (image,) = message.attachments
    assert (image.source, image.url, image.source_id) == ("graph", src, OBJECT)


def test_a_reply_hosted_copy_is_fetched_from_graph_too() -> None:
    src = "https://graph.microsoft.com/v1.0/teams/t1/channels/c1/messages/g1/replies/r1/hostedContents/h1/$value"
    image = inline_images.inline_image({"src": src, "itemid": OBJECT}).attachment
    assert image is not None and (image.source, image.url) == ("graph", src)


def test_other_graph_urls_naming_hosted_contents_are_never_fetched() -> None:
    for src in [
        "https://graph.microsoft.com/v1.0/me/messages?x=hostedContents/h1/$value",
        "https://graph.microsoft.com/v1.0/me/drive/root/children/hostedContents/h1/$value",
        "https://graph.microsoft.com/v1.0/teams/t1/channels/c1/messages/../../../../me/hostedContents/h1/$value",
        "https://graph.microsoft.com/v1.0/teams/t1/channels/c1/messages/g1/hostedContents/h1/$value?x=1",
    ]:
        image = inline_images.inline_image({"src": src, "itemid": OBJECT})
        assert image.attachment is not None and image.attachment.source is None, src
        assert image.attachment.url is None


def test_unique_name_numbers_only_taken_names() -> None:
    used = {"photo.png"}
    assert inline_images.unique_name("photo.png", used) == "photo (2).png"
    assert inline_images.unique_name("photo.png", used) == "photo (3).png"
    assert inline_images.unique_name("notes", used) == "notes"
    assert inline_images.unique_name("notes", used) == "notes (2)"
    assert inline_images.unique_name("Notes", {"notes"}) == "Notes (2)"
