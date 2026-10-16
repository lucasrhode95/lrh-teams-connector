"""Deleting the user's own messages: a set of messages in one chat, each checked again and deleted once.

The caller (MCP client) is responsible for the user's explicit confirmation of the whole set: it lists what
will be deleted and gets one yes. The connector cannot verify that confirmation. It checks the chat by its
current name, as a send does, and re-reads every message before its delete: only the user's own messages
qualify, so anything else in the set is skipped. A delete with an uncertain outcome is never resent; an
ambiguous outcome is reported per message. Files a deleted message shared are reported, not removed.
"""

from __future__ import annotations

from collections.abc import Sequence

from teams_connector.domain.errors import (
    AuthenticationRequired,
    ConnectorError,
    InvalidRequest,
    WriteOutcomeUnknown,
)
from teams_connector.domain.models import Chat, DeletedMessage, DeleteResult, Message, NotDeletedMessage
from teams_connector.remote.ports import MessageDeleter
from teams_connector.service.catalog import Catalog
from teams_connector.service.history import History
from teams_connector.service.send import normalized_name

MAX_DELETES = 100
DELETE_CHAT_KINDS = ("one_on_one", "group", "meeting", "self")  # Chat Service chats; channels are untried


class Deleter:
    def __init__(self, catalog: Catalog, history: History, deleter: MessageDeleter) -> None:
        self._catalog = catalog
        self._history = history
        self._deleter = deleter

    async def delete(
        self, conversation_id: str, *, expected_chat_name: str, message_ids: Sequence[str]
    ) -> DeleteResult:
        """Entry point: delete the user's own messages among ``message_ids`` in one chat. The ids and the
        chat name are validated here; nothing is deleted until both are valid."""
        ids = _check_ids(message_ids)
        if not expected_chat_name or not expected_chat_name.strip():
            raise InvalidRequest("expected_chat_name must be the chat's current name from list_chats.")
        chat = await self._catalog.recipient(conversation_id)
        if normalized_name(chat.name) != normalized_name(expected_chat_name):
            raise InvalidRequest(
                "expected_chat_name does not match the chat's current name; check the chat with list_chats."
            )
        if chat.kind not in DELETE_CHAT_KINDS:
            raise InvalidRequest("Messages can be deleted in chats only, not in channels.")
        result = DeleteResult(conversation_id=chat.id, chat_name=chat.name)
        for index, message_id in enumerate(ids):
            try:
                await self._delete_one(chat, message_id, result)
            except AuthenticationRequired as exc:
                if not result.deleted and not result.uncertain:
                    raise
                # Some deletes already happened: report them, and leave the rest for after sign-in.
                result.skipped.extend(
                    NotDeletedMessage(message_id=rest, reason=f"Not attempted: {exc}") for rest in ids[index:]
                )
                break
        return result

    async def _delete_one(self, chat: Chat, message_id: str, result: DeleteResult) -> None:
        """Read one message again and delete it once if the user wrote it; record the outcome."""
        try:
            message = await self._history.message(chat.id, message_id)
        except AuthenticationRequired:
            raise
        except ConnectorError as exc:
            result.skipped.append(NotDeletedMessage(message_id=message_id, reason=f"Not read: {exc}"))
            return
        reason = _not_deletable(message)
        if reason:
            result.skipped.append(NotDeletedMessage(message_id=message_id, reason=reason))
            return
        try:
            await self._deleter.delete(chat, message_id)
        except WriteOutcomeUnknown as exc:
            result.uncertain.append(NotDeletedMessage(message_id=message_id, reason=str(exc)))
            return
        except AuthenticationRequired:
            raise
        except ConnectorError as exc:
            result.skipped.append(NotDeletedMessage(message_id=message_id, reason=f"Teams refused: {exc}"))
            return
        result.deleted.append(
            DeletedMessage(message_id=message_id, sent_at=message.sent_at, files=_shared_files(message))
        )


def _check_ids(message_ids: Sequence[str]) -> list[str]:
    if not message_ids:
        raise InvalidRequest("Pass the ids of the messages to delete, from get_messages.")
    if len(message_ids) > MAX_DELETES:
        raise InvalidRequest(f"Delete at most {MAX_DELETES} messages with one call.")
    ids: list[str] = []
    for raw in message_ids:
        if not isinstance(raw, str) or not raw.strip():
            raise InvalidRequest("Each message id must be a non-empty string.")
        if raw.strip() not in ids:
            ids.append(raw.strip())
    return ids


def _not_deletable(message: Message) -> str | None:
    """Why a message is left alone: someone else's, a system event, or already deleted."""
    if message.is_self is not True:
        return "Not your message."
    if message.system_event:
        return "A system event, not a message."
    if message.is_deleted:
        return "Already deleted."
    return None


def _shared_files(message: Message) -> list[str]:
    """The files a message shared from OneDrive or SharePoint, with or without a link we can follow."""
    return [item.name for item in message.attachments if item.kind == "file" and item.source != "loop"]
