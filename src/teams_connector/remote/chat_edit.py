"""Private Chat Service edits and AMS image uploads, with one attempt per write.

The service validates authorization inputs, ownership, paths and HTML. This adapter retains structured
source only for one call, preserves untouched file records and image tags, and checks for intervening edits
before PUT. A successful PUT followed by an unreadable/mismatched readback is accepted but unverified.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from html import escape
from html.parser import HTMLParser
from pathlib import PurePath
from typing import Any
from urllib.parse import quote, urlsplit

from teams_connector.config import AMS_CLIENT_VERSION
from teams_connector.domain.errors import (
    AuthenticationRequired,
    ConnectorError,
    InvalidRequest,
    NotFound,
    Upstream,
    WriteOutcomeUnknown,
)
from teams_connector.domain.models import Chat
from teams_connector.remote import chat_service_mapping as mapping
from teams_connector.remote import inline_images
from teams_connector.remote.image_readers import AmsCredential
from teams_connector.remote.ports import EditReceipt, HostedImage, UploadedFile, UploadedImage
from teams_connector.remote.teams_session import SkypeCredential, TeamsSession
from teams_connector.remote.transport import Credential, Transport, operation

# Graph documents a chat message limit of about 28 KB, text, images and links included.
MAX_CONTENT_BYTES = 28_000
# Room checked before upload for each new image tag, whose AMS address is not known yet.
NEW_IMAGE_TAG_RESERVE = 512


class ChatEditor:
    def __init__(self, reader: Transport, writer: Transport, session: TeamsSession) -> None:
        """``writer`` is a Transport(max_attempts=1) sharing the reader's HTTP client."""
        self._reader, self._writer, self._session = reader, writer, session
        self._credential = SkypeCredential(session)

    async def prepare(self, chat: Chat, message_id: str) -> ChatEdit:
        base = await self._session.chat_service_url()
        messages = f"{base}/v1/users/ME/conversations/{quote(chat.id, safe='')}/messages"
        url = f"{messages}/{quote(message_id, safe='')}"
        with operation("preparing a message edit"):
            raw = await self._reader.json("GET", url, credential=self._credential)
        if not isinstance(raw, dict) or str(raw.get("id", "")) != message_id:
            raise NotFound("Teams returned no matching message for that id.")
        return ChatEdit(self._reader, self._writer, self._credential, url, chat.id, raw)

    async def upload_image(self, chat: Chat, name: str, image: HostedImage) -> UploadedImage:
        info = await self._session.info()
        if not info.ams_url or not info.ams_upload_url:
            raise Upstream("Teams AuthZ did not return valid regional AMS image routes.")
        credential = OnceCredential(AmsCredential(self._session))
        with operation("uploading an edit image"):
            raw = await self._writer.json(
                "POST",
                info.ams_upload_url + "/v1/objects/",
                credential=credential,
                headers={"X-Client-Version": AMS_CLIENT_VERSION},
                json_body={
                    "type": "pish/image",
                    "permissions": {chat.id: ["read"]},
                    "sharingMode": "Inline",
                    "filename": name,
                },
                retry=False,
            )
            identifier = raw.get("id") if isinstance(raw, dict) else None
            if not isinstance(identifier, str) or not inline_images.AMS_OBJECT_ID.fullmatch(identifier):
                raise WriteOutcomeUnknown(
                    "AMS created an image without a usable id; check before uploading again."
                )
            result = UploadedImage(
                name,
                identifier,
                f"{info.ams_url}/v1/objects/{identifier}/views/imgo",
                f"{info.ams_upload_url}/v1/objects/{identifier}",
            )
            try:
                await self._writer.request(
                    "PUT",
                    result.object_url + "/content/imgpsh",
                    credential=credential,
                    headers={"Content-Type": "application/octet-stream"},
                    content=image.data,
                    retry=False,
                )
            except WriteOutcomeUnknown:
                raise WriteOutcomeUnknown(
                    "The image upload was not confirmed; an unused image may remain. Check before retrying."
                ) from None
            except ConnectorError as exc:
                try:
                    await self.discard_image(result)
                except ConnectorError:
                    raise Upstream(
                        "Image upload failed and its new AMS object could not be removed."
                    ) from exc
                raise
        return result

    async def discard_image(self, image: UploadedImage) -> None:
        await self._writer.request(
            "DELETE", image.object_url, credential=OnceCredential(AmsCredential(self._session)), retry=False
        )


class ChatEdit:
    def __init__(
        self,
        reader: Transport,
        writer: Transport,
        credential: Credential,
        url: str,
        conversation_id: str,
        raw: dict[str, Any],
    ) -> None:
        self.message = mapping.to_message(raw, conversation_id)
        self._reader, self._writer, self._credential = reader, writer, credential
        self._url, self._conversation_id, self._raw = url, conversation_id, raw
        source = str(raw.get("content") or "")
        if (
            "html" not in str(raw.get("messagetype", "")).lower()
            and "html" not in str(raw.get("contenttype", "")).lower()
        ):
            source = "<p>" + escape(source, quote=False).replace("\n", "<br>") + "</p>"
        self._media = SourceImages(source)
        self._files = file_records(raw)
        self._file_models = [a for a in self.message.attachments if a.kind in ("file", "link")]
        if len(self._file_models) != len(self._files):
            raise InvalidRequest("This message's attachment structure cannot be edited safely.")
        self._images = [a for a in self.message.attachments if a.kind == "image"]
        if len(self._images) != len(self._media.tags):
            raise InvalidRequest("This message's image structure cannot be edited safely.")

    def check(self, body: str | None, *, new_images: int, remove_names: Sequence[str]) -> None:
        """Refuse an edit whose content would be too large, before anything is uploaded. Each new image is
        measured with ``NEW_IMAGE_TAG_RESERVE`` characters for its address."""
        reserve = "x" * NEW_IMAGE_TAG_RESERVE
        _check_size(self._content(body, [f"{reserve}{i}" for i in range(new_images)], remove_names))

    async def replace(
        self,
        body: str | None,
        *,
        files: Sequence[UploadedFile],
        images: Sequence[UploadedImage],
        remove_names: Sequence[str],
    ) -> EditReceipt:
        """Internal contract: inputs and ownership validated; every new upload belongs to this edit."""
        payload = self._replacement(body, files, images, remove_names)
        expected = mapping.to_message(payload, self._conversation_id)
        with operation("editing a message"):
            current = await self._reader.json("GET", self._url, credential=self._credential)
            if not isinstance(current, dict) or _snapshot(current) != _snapshot(self._raw):
                raise InvalidRequest(
                    "The message changed while preparing the edit; read it again before editing."
                )
            await self._writer.request(
                "PUT", self._url, credential=OnceCredential(self._credential), json_body=payload, retry=False
            )
        try:
            actual = await self._reader.json("GET", self._url, credential=self._credential)
        except ConnectorError:
            return EditReceipt(
                False,
                reason="Teams accepted the edit but readback failed; check get_messages before retrying.",
            )
        if not isinstance(actual, dict):
            return EditReceipt(False, reason="Teams accepted the edit but returned no readable confirmation.")
        try:
            message = mapping.to_message(actual, self._conversation_id)
        except (ConnectorError, ValueError, TypeError):
            return EditReceipt(
                False, reason="Teams accepted the edit but its confirmation could not be parsed."
            )
        verified = (
            actual.get("id") == payload.get("id")
            and message.text == expected.text
            and message.author_id == self.message.author_id
            and message.edited_at is not None
            and (self.message.edited_at is None or message.edited_at > self.message.edited_at)
            and not message.is_deleted
            and message.attachments == expected.attachments
        )
        return EditReceipt(
            verified,
            message,
            None
            if verified
            else "Teams accepted the edit but readback differs; check get_messages before retrying.",
        )

    def _replacement(
        self,
        body: str | None,
        files: Sequence[UploadedFile],
        images: Sequence[UploadedImage],
        remove_names: Sequence[str],
    ) -> dict[str, Any]:
        removed = set(remove_names)
        content = self._content(body, [image.view_url for image in images], remove_names)
        _check_size(content)
        payload = copy.deepcopy(self._raw)
        props = mapping.properties(payload).copy()
        kept = [
            record
            for record, model in zip(self._files, self._file_models, strict=True)
            if model.name not in removed
        ]
        props["files"] = json.dumps([*kept, *(_file_record(file) for file in files)])
        previous = int(self.message.edited_at.timestamp() * 1000) if self.message.edited_at else 0
        props["edittime"] = str(max(int(datetime.now(UTC).timestamp() * 1000), previous + 1))
        if body is not None:
            for field in ("mentions", "cards", "links"):
                if field in props:
                    props[field] = "[]" if isinstance(props[field], str) else []
        # An AMS object stays referenced while any retained image tag still shows it.
        retained_ids = {a.source_id for a in self._images if a.name not in removed}
        removed_ids = {a.source_id for a in self._images if a.name in removed} - retained_ids
        refs = _array(payload.get("amsreferences", []))
        payload.update(
            content=content,
            contenttype="text",
            messagetype="RichText/Html",
            properties=props,
            amsreferences=[*[r for r in refs if r not in removed_ids], *[i.object_id for i in images]],
        )
        return payload

    def _content(self, body: str | None, image_urls: Sequence[str], remove_names: Sequence[str]) -> str:
        """The edited HTML: the new body (or the old one without removed images and their files' tags),
        retained image tags, then each new image the body does not already place."""
        removed = set(remove_names)
        removed_indices = {i for i, a in enumerate(self._images) if a.name in removed}
        if body is None:
            removed_files = {
                str(record.get("itemid") or record.get("id") or "")
                for record, model in zip(self._files, self._file_models, strict=True)
                if model.name in removed
            }
            content = self._media.without(removed_indices, removed_files - {""})
        else:
            retained = [tag for i, (_, _, tag, _) in enumerate(self._media.tags) if i not in removed_indices]
            content = body + "".join(f"<p>{tag}</p>" for tag in retained)
        present = SourceImages(content).sources
        return content + "".join(
            f'<p><img src="{escape(url, quote=True)}"></p>' for url in image_urls if url not in present
        )


class OnceCredential:
    """Prevent the transport's usual 401 renewal from issuing a second edit/upload request."""

    def __init__(self, credential: Credential) -> None:
        self._credential = credential

    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        if renewal is not None:
            raise AuthenticationRequired(
                "Microsoft rejected this write's sign-in; it was not repeated.",
                command=self.sign_in_command(),
            )
        return await self._credential.headers(None)

    def sign_in_command(self) -> str:
        return self._credential.sign_in_command()


class SourceImages(HTMLParser):
    """Image references and byte ranges from source HTML, never rendered attachment markers."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.tags: list[tuple[int, int, str, str]] = []
        self.placeholders: list[tuple[int, int, str]] = []
        self._placeholder: tuple[int, str] | None = None
        self.feed(source)
        # HTMLParser holds back trailing text that may end in a character reference; only a held-back
        # tag means the markup is unfinished.
        if "<" in self.rawdata or self._placeholder is not None:
            raise InvalidRequest("The existing message has incomplete markup and cannot be edited safely.")

    @property
    def sources(self) -> set[str]:
        return {src for _, _, _, src in self.tags}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"uriobject", "swift"}:
            raise InvalidRequest("Edits of embedded cards are not supported.")
        if tag == "attachment":
            self._placeholder = (self._offset(), dict(attrs).get("id") or "")
            return
        if tag != "img":
            return
        attributes = {k: v or "" for k, v in attrs}
        image = inline_images.inline_image(attributes)
        if image.attachment is None:
            if image.name is not None:
                raise InvalidRequest("The existing message has an unsupported image reference.")
            return
        start = self._offset()
        text = self.get_starttag_text() or ""
        self.tags.append((start, start + len(text), text, attributes.get("src", "")))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag == "attachment" and self._placeholder is not None:
            start, identifier = self._placeholder
            self.placeholders.append((start, start + len(self.get_starttag_text() or ""), identifier))
            self._placeholder = None

    def handle_endtag(self, tag: str) -> None:
        if tag == "attachment" and self._placeholder is not None:
            start, identifier = self._placeholder
            self.placeholders.append((start, self.source.index(">", self._offset()) + 1, identifier))
            self._placeholder = None

    def _offset(self) -> int:
        line, column = self.getpos()
        return sum(len(s) + 1 for s in self.source.split("\n")[: line - 1]) + column

    def without(self, indices: set[int], file_ids: set[str]) -> str:
        """The source without the image tags at ``indices`` and the ``<attachment>`` tags of the removed
        files ``file_ids``; other tags are kept. Nested or overlapping ranges are cut once."""
        result, cursor = [], 0
        ranges = [
            *[(start, end) for start, end, identifier in self.placeholders if identifier in file_ids],
            *[(start, end) for i, (start, end, _, _) in enumerate(self.tags) if i in indices],
        ]
        for start, end in sorted(ranges):
            if end <= cursor:
                continue
            result.append(self.source[cursor : max(start, cursor)])
            cursor = end
        return "".join(result) + self.source[cursor:]


def file_records(raw: dict[str, Any]) -> list[dict[str, Any]]:
    records = _array(mapping.properties(raw).get("files", []))
    if any(not isinstance(record, dict) for record in records):
        raise InvalidRequest("The existing message has unsupported file metadata.")
    return records


def _array(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise InvalidRequest("The existing message has invalid attachment metadata.") from None
    if not isinstance(value, list):
        raise InvalidRequest("The existing message has unsupported attachment metadata.")
    return value


def _file_record(file: UploadedFile) -> dict[str, Any]:
    parts = urlsplit(file.web_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    extension = PurePath(file.name).suffix.removeprefix(".").lower()
    return {
        "itemid": file.attachment_id,
        "id": file.attachment_id,
        "fileName": file.name,
        "title": file.name,
        "fileType": extension,
        "type": extension,
        "@type": "http://schema.skype.com/File",
        "version": 2,
        "state": "active",
        "baseUrl": origin,
        "objectUrl": file.web_url,
        "fileInfo": {
            "itemId": file.item_id,
            "fileUrl": file.web_url,
            "shareUrl": file.web_url,
            "siteUrl": origin,
        },
        "sharepointIds": {"driveId": file.drive_id, "listItemUniqueId": file.attachment_id},
    }


def _check_size(content: str) -> None:
    if len(content.encode()) > MAX_CONTENT_BYTES:
        raise InvalidRequest(
            f"The edited message would exceed about {MAX_CONTENT_BYTES:,} bytes; "
            "shorten it or keep fewer images."
        )


def _snapshot(raw: dict[str, Any]) -> tuple[Any, ...]:
    return (
        raw.get("id"),
        raw.get("from"),
        raw.get("content"),
        raw.get("version"),
        mapping.properties(raw),
        raw.get("amsreferences"),
    )
