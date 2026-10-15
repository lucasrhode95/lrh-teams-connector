"""Sending a message: validated against the live catalog, attempted exactly once.

The caller (MCP client) is responsible for the user's explicit authorization of the recipient, the
content and any files. The send checks the recipient: ``expected_chat_name`` must match the chat's
current name.

The body is either plain text, escaped and sent as paragraphs, or HTML, checked for active and unfinished
markup and sent as written. Inline images (PNG or JPEG) go in the same request: an ``<img src="name">`` in
the HTML names one by file name, and images the body does not name follow it. Files (one-on-one and group
chats, and the self chat) are uploaded to the sender's OneDrive and shared with the chat's other members
first; the message is posted only when every file was uploaded and shared. Files uploaded before a failure
are reported, never deleted, and nothing is retried.
"""

from __future__ import annotations

import html as markup
import math
from collections.abc import Sequence
from pathlib import Path

from teams_connector import config
from teams_connector.domain.errors import ConnectorError, InvalidRequest, Upstream, WriteOutcomeUnknown
from teams_connector.domain.models import Chat, SendResult, SentFile
from teams_connector.remote.ports import (
    ChatWriter,
    FileSharer,
    HostedImage,
    MemberReader,
    UploadedFile,
    hosted_image_src,
)
from teams_connector.service.catalog import Catalog
from teams_connector.service.passive_html import check_passive_html, with_hosted_images

MAX_TEXT_CHARS = 10_000
MAX_FILES = 10
FILE_CHAT_KINDS = ("one_on_one", "group", "self")  # where files are supported; channels and meetings later
MAX_IMAGES = 10
# Graph refuses a message request over about 4 MiB (live, 2026-10-15: 4.08 MB passed, 4.47 MB did not), and
# base64 adds a third to each image, so a message's images total under about 3 MB.
MAX_IMAGE_REQUEST_BYTES = 4 * 1024 * 1024
IMAGE_JSON_OVERHEAD = 200  # bytes per hostedContents entry besides its base64 data
_IMAGE_SIGNATURES = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg"}
_GUID_CHARS = set("0123456789abcdef-")


class Sender:
    def __init__(
        self,
        catalog: Catalog,
        writer: ChatWriter,
        *,
        sharer: FileSharer,
        members: MemberReader,
        me_id: str,
    ) -> None:
        self._catalog = catalog
        self._writer = writer
        self._sharer = sharer
        self._members = members
        self._me_id = me_id.lower()

    async def send(
        self,
        conversation_id: str,
        *,
        expected_chat_name: str,
        text: str | None = None,
        html: str | None = None,
        files: Sequence[str] | None = None,
        images: Sequence[str] | None = None,
    ) -> SendResult:
        """Entry point: one message, as plain ``text`` or as ``html``, optionally with local ``files`` and
        inline ``images`` (absolute paths). Without files or images exactly one of text or html is
        required; with them, at most one. Every argument is validated here, image sizes included; nothing
        is uploaded or sent until all of it is valid."""
        local_files = _check_files(files or [])
        inline = _check_images(images or [])
        if (text is not None and html is not None) or (
            text is None and html is None and not local_files and not inline
        ):
            raise InvalidRequest(
                "Pass exactly one of text (plain text) or html (HTML, sent as written); "
                "with files or images, both may be omitted."
            )
        named: set[str] = set()
        if text is not None:
            _check_text(text)
            body = _paragraphs(text)
        elif html is not None:
            body, named = _check_html(html, inline)
        else:
            body = ""
        body = _with_images(body, inline, named)
        _check_request_size(body, inline)
        if not expected_chat_name or not expected_chat_name.strip():
            raise InvalidRequest("expected_chat_name must be the chat's current name from list_chats.")
        chat = await self._catalog.recipient(conversation_id)  # the live name of this recipient only
        if _normalized_name(chat.name) != _normalized_name(expected_chat_name):
            raise InvalidRequest(
                "expected_chat_name does not match the chat's current name; "
                "check the recipient with list_chats."
            )
        content = text if text is not None else (html or "")
        uploaded: list[UploadedFile] = []
        if local_files:
            recipients = await self._recipients(chat)
            uploaded = await self._upload_and_share(local_files, recipients)
        try:
            receipt = await self._writer.send(
                chat, body, attachments=uploaded, images=[image for _, image in inline]
            )
        except ConnectorError as exc:
            if not uploaded:
                raise
            raise _with_uploads(exc, uploaded, shared=True, action="The message was not posted") from exc
        return SendResult(
            conversation_id=chat.id,
            chat_name=chat.name,
            message_id=receipt.message_id,
            sent_at=receipt.sent_at,
            characters=len(content),
            files=[SentFile(name=f.name, url=f.web_url) for f in uploaded],
        )

    async def _recipients(self, chat: Chat) -> list[str]:
        """The chat's other people, by Entra object id. Apps (``28:``) are left out. Anyone else without an
        object id in this directory (no id at all, or a federated or anonymous member) cannot be granted
        access, so the send is refused rather than posting a file someone cannot open. The self chat has
        nobody to share with."""
        if chat.kind not in FILE_CHAT_KINDS:
            raise InvalidRequest("Files can be sent to one-on-one and group chats and the self chat only.")
        if chat.kind == "self":
            return []
        people = []
        for member in await self._members.members(chat):
            member_id = (member.id or "").lower()
            if member_id == self._me_id or member_id.startswith("28:"):
                continue
            if not _is_object_id(member_id):
                raise InvalidRequest(
                    "This chat has a member outside your organization's directory; "
                    "files cannot be shared with them."
                )
            people.append(member_id)
        if not people:
            raise InvalidRequest("This chat has no other members to share files with.")
        return people

    async def _upload_and_share(self, paths: list[Path], recipients: list[str]) -> list[UploadedFile]:
        """Upload every file, then share each (unless nobody else is in the chat). Each call is made once; on
        a failure the files uploaded so far are named in the error and left in place."""
        uploaded: list[UploadedFile] = []
        for path in paths:
            try:
                uploaded.append(await self._sharer.upload(path))
            except ConnectorError as exc:
                if not uploaded and not isinstance(exc, WriteOutcomeUnknown):
                    raise
                raise _with_uploads(
                    exc,
                    uploaded,
                    shared=False,
                    action=f"Uploading {path.name} failed; nothing was posted",
                    uncertain=path.name if isinstance(exc, WriteOutcomeUnknown) else None,
                ) from exc
        for file in uploaded if recipients else []:
            try:
                await self._sharer.share(file, recipients)
            except ConnectorError as exc:
                raise _with_uploads(
                    exc, uploaded, shared=False, action=f"Sharing {file.name} failed; nothing was posted"
                ) from exc
        return uploaded


def _check_files(files: Sequence[str]) -> list[Path]:
    """Local files to send: absolute paths of readable regular files within config.MAX_SEND_FILE_BYTES."""
    if len(files) > MAX_FILES:
        raise InvalidRequest(f"Send at most {MAX_FILES} files with one message.")
    paths = []
    for raw in files:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise InvalidRequest("Each file must be a non-empty path.")
        path = Path(raw)
        if not path.is_absolute():
            raise InvalidRequest(f"File paths must be absolute: {raw}")
        if not path.is_file():
            raise InvalidRequest(f"Not a readable file: {raw}")
        if path.stat().st_size > config.MAX_SEND_FILE_BYTES:
            raise InvalidRequest(
                f"{path.name} is too large to send: the limit is {_size(config.MAX_SEND_FILE_BYTES)} "
                "(config.MAX_SEND_FILE_BYTES)."
            )
        paths.append(path)
    return paths


def _check_images(images: Sequence[str]) -> list[tuple[str, HostedImage]]:
    """Inline images: absolute paths of PNG or JPEG files (by their content), with distinct file names,
    since the HTML names them by file name. Returns (file name, image) in the order given."""
    if len(images) > MAX_IMAGES:
        raise InvalidRequest(f"Send at most {MAX_IMAGES} images with one message.")
    checked: list[tuple[str, HostedImage]] = []
    for raw in images:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise InvalidRequest("Each image must be a non-empty path.")
        path = Path(raw)
        if not path.is_absolute():
            raise InvalidRequest(f"Image paths must be absolute: {raw}")
        if not path.is_file():
            raise InvalidRequest(f"Not a readable file: {raw}")
        if any(name == path.name for name, _ in checked):
            raise InvalidRequest(f"Two images are named {path.name}; the HTML names images by file name.")
        if path.stat().st_size > MAX_IMAGE_REQUEST_BYTES:
            raise _too_large_for_inline()
        data = path.read_bytes()
        kind = next((kind for magic, kind in _IMAGE_SIGNATURES.items() if data.startswith(magic)), None)
        if kind is None:
            raise InvalidRequest(
                f"{path.name} is not a PNG or JPEG image; Teams shows only those inline. "
                "Send it in files instead."
            )
        checked.append((path.name, HostedImage(data=data, content_type=kind)))
    return checked


def _with_images(body: str, images: list[tuple[str, HostedImage]], named: set[str]) -> str:
    """The body with every image it does not already name appended after it, one paragraph each."""
    return body + "".join(
        f'<p><img src="{hosted_image_src(number)}"></p>'
        for number, (name, _) in enumerate(images, start=1)
        if name not in named
    )


def _check_request_size(body: str, images: list[tuple[str, HostedImage]]) -> None:
    """Refuse images that would push the request past Graph's limit, measured as sent (base64)."""
    if not images:
        return
    size = len(body.encode("utf-8")) + sum(
        4 * math.ceil(len(image.data) / 3) + IMAGE_JSON_OVERHEAD for _, image in images
    )
    if size > MAX_IMAGE_REQUEST_BYTES:
        raise _too_large_for_inline()


def _too_large_for_inline() -> InvalidRequest:
    return InvalidRequest(
        "The images are too large to send inline: Teams takes about 4 MiB per message, so about 3 MB of "
        "images in total. Send them in files instead."
    )


def _paragraphs(text: str) -> str:
    """Plain text as one paragraph: escaped, with each line break kept as ``<br>``."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "<p>" + "<br>".join(markup.escape(line, quote=False) for line in lines) + "</p>"


def _size(value: int) -> str:
    return f"{value / 1024**3:g} GiB" if value >= 1024**3 else f"{value / 1024**2:g} MiB"


def _with_uploads(
    exc: ConnectorError,
    uploaded: list[UploadedFile],
    *,
    shared: bool,
    action: str,
    uncertain: str | None = None,
) -> ConnectorError:
    """The failure, naming the files already in the sender's OneDrive so the user can decide about them.
    An unknown outcome stays WriteOutcomeUnknown; anything else is reported as an upstream failure."""
    state = "uploaded and shared" if shared else "uploaded"
    listed = "; ".join(f"{f.name} ({f.web_url})" for f in uploaded)
    message = f"{action}: {exc}"
    if listed:
        message += f" Already {state} to your OneDrive and not deleted: {listed}."
    if uncertain:
        message += f" {uncertain} may also have been uploaded."
    if isinstance(exc, WriteOutcomeUnknown):
        return WriteOutcomeUnknown(message)
    return Upstream(message)


def _is_object_id(value: str) -> bool:
    return len(value) == 36 and set(value) <= _GUID_CHARS


def _normalized_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _check_text(text: str) -> None:
    if not text or not text.strip():
        raise InvalidRequest("text must be a non-empty string.")
    if len(text) > MAX_TEXT_CHARS:
        raise InvalidRequest(f"text must contain at most {MAX_TEXT_CHARS:,} characters.")
    if "\x00" in text:
        raise InvalidRequest("text cannot contain a null character.")


def _check_html(html: str, images: list[tuple[str, HostedImage]]) -> tuple[str, set[str]]:
    """HTML is measured and checked as it is sent. Returns it with each ``<img>`` that names one of
    ``images`` pointed at its hosted copy, and the names of those images."""
    if not html or not html.strip():
        raise InvalidRequest("html must be a non-empty string.")
    if len(html) > MAX_TEXT_CHARS:
        raise InvalidRequest(f"html must contain at most {MAX_TEXT_CHARS:,} characters.")
    if "\x00" in html:
        raise InvalidRequest("html cannot contain a null character.")
    numbers = {name: hosted_image_src(number) for number, (name, _) in enumerate(images, start=1)}
    tags = check_passive_html(html, numbers.keys())
    return with_hosted_images(html, tags, numbers), {tag.src for tag in tags}
