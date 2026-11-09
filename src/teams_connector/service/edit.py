"""One explicitly authorized edit of the user's own chat message, including attachments.

Public inputs are validated here before uploads. New files/images are added, named attachments can be
removed, and everything else is retained. Removed attachments remain in their source storage. Only new
uploads are rolled back after a definite failure; an uncertain edit retains them and is never repeated.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from uuid import UUID

from teams_connector.domain.errors import ConnectorError, InvalidRequest, Upstream, WriteOutcomeUnknown
from teams_connector.domain.models import Chat, EditResult
from teams_connector.remote.ports import FileSharer, MemberReader, MessageEditor, UploadedFile, UploadedImage
from teams_connector.service.catalog import Catalog
from teams_connector.service.hosted_images import ImageTag, check_hosted_images, with_hosted_images
from teams_connector.service.send import check_files, check_images, check_text, normalized_name

EDIT_KINDS = ("self", "one_on_one", "group", "meeting")
FILE_KINDS = ("self", "one_on_one", "group")


class Editor:
    def __init__(
        self,
        catalog: Catalog,
        editor: MessageEditor,
        sharer: FileSharer,
        members: MemberReader,
        me_id: str,
    ) -> None:
        self._catalog, self._editor, self._sharer, self._members = catalog, editor, sharer, members
        self._me_id = me_id.lower()

    async def edit(
        self,
        conversation_id: str,
        *,
        expected_chat_name: str,
        message_id: str,
        text: str | None = None,
        html: str | None = None,
        files: Sequence[str] = (),
        images: Sequence[str] = (),
        remove_attachments: Sequence[str] = (),
    ) -> EditResult:
        """Entry point. The user must authorize this message, new body and every attachment change.
        Omitted text/html preserves the body. Files/images add uploads; remove_attachments names existing
        attachments from get_messages. Removing a reference never deletes its source file.
        """
        if not isinstance(message_id, str) or not message_id.strip() or any(ord(c) < 32 for c in message_id):
            raise InvalidRequest("message_id must be a non-empty id from get_messages.")
        if not expected_chat_name or not expected_chat_name.strip():
            raise InvalidRequest("expected_chat_name must be the current name from list_chats.")
        if text is not None and html is not None:
            raise InvalidRequest("Pass at most one of text or html.")
        if text is None and html is None and not files and not images and not remove_attachments:
            raise InvalidRequest("Pass a new body, files, images, or attachment names to remove.")
        local, inline = check_files(files), check_images(images)
        body: str | None = None
        tags: list[ImageTag] = []
        if text is not None:
            check_text(text)
            lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            body = "<p>" + "<br>".join(escape(line, quote=False) for line in lines) + "</p>"
        elif html is not None:
            check_text(html)
            tags = check_hosted_images(html, {name for name, _ in inline})
            body = html
        removals = _names(remove_attachments)
        chat = await self._catalog.recipient(conversation_id)
        if normalized_name(chat.name) != normalized_name(expected_chat_name):
            raise InvalidRequest(
                "expected_chat_name does not match the chat's current name; check list_chats."
            )
        if chat.kind not in EDIT_KINDS:
            raise InvalidRequest("Messages can be edited in chats only, not in channels.")
        target = await self._editor.prepare(chat, message_id.strip())
        message = target.message
        if message.author_id != self._me_id:
            raise InvalidRequest("Not your message.")
        if message.is_deleted or message.system_event or message.is_recording_card:
            raise InvalidRequest("Deleted messages, system events and recording cards cannot be edited.")
        known = {attachment.name for attachment in message.attachments}
        if any(name not in known for name in removals):
            raise InvalidRequest("An attachment name to remove was not found; use names from get_messages.")
        remaining = [a for a in message.attachments if a.name not in removals]
        if (
            sum(a.kind == "file" for a in remaining) + len(local) > 10
            or sum(a.kind == "image" for a in remaining) + len(inline) > 10
        ):
            raise InvalidRequest("The edited message may contain at most 10 files and 10 inline images.")
        if (local or inline or removals) and chat.kind not in FILE_KINDS:
            raise InvalidRequest("Attachment changes support self, one-on-one and group chats only.")
        uploaded: list[UploadedFile] = []
        uploaded_images: list[UploadedImage] = []
        edit_started = False
        try:
            recipients = await self._recipients(chat) if local else []
            for path in local:
                file = await self._sharer.upload(path)
                uploaded.append(file)
                if recipients:
                    await self._sharer.share(file, recipients)
            for name, image in inline:
                uploaded_images.append(await self._editor.upload_image(chat, name, image))
            if body is not None and uploaded_images:
                body = with_hosted_images(
                    body, tags, {image.name: image.view_url for image in uploaded_images}
                )
            edit_started = True
            receipt = await target.replace(
                body, files=uploaded, images=uploaded_images, remove_names=removals
            )
        except WriteOutcomeUnknown:
            if not edit_started:
                await self._rollback(uploaded, uploaded_images)
            raise WriteOutcomeUnknown(
                "The edit or an upload was not confirmed; new uploads may remain. "
                "Check get_messages and your uploaded files before retrying. "
                "The edit request was not repeated."
            ) from None
        except ConnectorError as exc:
            failures = await self._rollback(uploaded, uploaded_images)
            if failures:
                raise Upstream(
                    f"The edit was not applied; {failures} new upload(s) could not be removed. "
                    "Check your files."
                ) from exc
            raise
        actual = receipt.message
        return EditResult(
            conversation_id=chat.id,
            chat_name=chat.name,
            message_id=message.id,
            verified=receipt.verified,
            status="updated" if receipt.verified else "accepted",
            edited_at=actual.edited_at if actual else None,
            attachments=actual.attachments if actual else [],
            reason=receipt.reason,
        )

    async def _recipients(self, chat: Chat) -> list[str]:
        if chat.kind == "self":
            return []
        recipients = []
        for member in await self._members.members(chat):
            identifier = (member.id or "").lower()
            if identifier == self._me_id or identifier.startswith("28:"):
                continue
            try:
                identifier = str(UUID(identifier))
            except ValueError:
                raise InvalidRequest(
                    "A chat member has no directory id; a new file cannot be shared safely."
                ) from None
            recipients.append(identifier)
        if not recipients:
            raise InvalidRequest("The other chat members could not be resolved for sharing a new file.")
        return list(dict.fromkeys(recipients))

    async def _rollback(self, files: Sequence[UploadedFile], images: Sequence[UploadedImage]) -> int:
        failures = 0
        for file in files:
            try:
                await self._sharer.delete(file)
            except ConnectorError:
                failures += 1
        for image in images:
            try:
                await self._editor.discard_image(image)
            except ConnectorError:
                failures += 1
        return failures


def _names(names: Sequence[str]) -> list[str]:
    if len(names) > 20 or any(not isinstance(name, str) or not name.strip() for name in names):
        raise InvalidRequest("Pass up to 20 non-empty attachment names from get_messages.")
    return list(dict.fromkeys(names))
