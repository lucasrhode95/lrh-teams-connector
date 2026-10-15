"""Sending a message: validated against the live catalog, attempted exactly once.

The caller (MCP client) is responsible for the user's explicit authorization of the recipient, the
content and any files. The send checks the recipient: ``expected_chat_name`` must match the chat's
current name.

The body is either plain text, escaped and sent as paragraphs, or HTML, checked for active and unfinished
markup and sent as written. Files (one-on-one and group chats) are uploaded to the sender's OneDrive and
shared with the chat's other members first; the message is posted only when every file was uploaded and
shared. Files uploaded before a failure are reported, never deleted, and nothing is retried.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from teams_connector import config
from teams_connector.domain.errors import ConnectorError, InvalidRequest, Upstream, WriteOutcomeUnknown
from teams_connector.domain.models import Chat, SendResult, SentFile
from teams_connector.remote.ports import ChatWriter, FileSharer, MemberReader, UploadedFile
from teams_connector.service.catalog import Catalog
from teams_connector.service.passive_html import check_passive_html

MAX_TEXT_CHARS = 10_000
MAX_FILES = 10
FILE_CHAT_KINDS = ("one_on_one", "group")  # where files are supported; channels and meetings come later
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
    ) -> SendResult:
        """Entry point: one message, as plain ``text`` or as ``html``, optionally with local ``files``
        (absolute paths). Without files exactly one of text or html is required; with files, at most one.
        Every argument is validated here; nothing is uploaded or sent until all of it is valid."""
        local_files = _check_files(files or [])
        if (text is not None and html is not None) or (text is None and html is None and not local_files):
            raise InvalidRequest(
                "Pass exactly one of text (plain text) or html (HTML, sent as written); "
                "with files, both may be omitted."
            )
        if text is not None:
            _check_text(text)
        elif html is not None:
            _check_html(html)
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
            receipt = await self._writer.send(chat, content, as_html=text is None, attachments=uploaded)
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
        access, so the send is refused rather than posting a file someone cannot open."""
        if chat.kind not in FILE_CHAT_KINDS:
            raise InvalidRequest("Files can be sent to one-on-one and group chats only.")
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
        """Upload every file, then share each. Each call is made once; on a failure the files uploaded so
        far are named in the error and left in place."""
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
        for file in uploaded:
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


def _check_html(html: str) -> None:
    """HTML is measured and checked as it is sent."""
    if not html or not html.strip():
        raise InvalidRequest("html must be a non-empty string.")
    if len(html) > MAX_TEXT_CHARS:
        raise InvalidRequest(f"html must contain at most {MAX_TEXT_CHARS:,} characters.")
    if "\x00" in html:
        raise InvalidRequest("html cannot contain a null character.")
    check_passive_html(html)
