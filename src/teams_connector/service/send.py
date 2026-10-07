"""Sending a message: validated against the live catalog, attempted exactly once.

The caller (MCP client) is responsible for the user's explicit authorization of the recipient and
the content; ``user_confirmation`` records it but is not permission by itself. The value is neither
logged nor stored.
"""

from __future__ import annotations

from teams_connector.domain.errors import InvalidRequest
from teams_connector.domain.models import SendResult
from teams_connector.remote.ports import ChatWriter
from teams_connector.service.catalog import Catalog

MAX_TEXT_CHARS = 10_000
MAX_CONFIRMATION_CHARS = 1_000


def _normalized_name(value: str) -> str:
    return " ".join(value.split()).casefold()


class Sender:
    def __init__(self, catalog: Catalog, writer: ChatWriter) -> None:
        self._catalog = catalog
        self._writer = writer

    async def send(
        self, conversation_id: str, *, expected_chat_name: str, text: str, user_confirmation: str
    ) -> SendResult:
        if not expected_chat_name or not expected_chat_name.strip():
            raise InvalidRequest("expected_chat_name must be the chat's current name from list_chats.")
        if not text or not text.strip():
            raise InvalidRequest("text must be a non-empty string.")
        if len(text) > MAX_TEXT_CHARS:
            raise InvalidRequest(f"text must contain at most {MAX_TEXT_CHARS:,} characters.")
        if "\x00" in text:
            raise InvalidRequest("text cannot contain a null character.")
        if (
            not user_confirmation
            or not user_confirmation.strip()
            or len(user_confirmation) > MAX_CONFIRMATION_CHARS
        ):
            raise InvalidRequest(
                "user_confirmation must record the user's explicit authorization to send this."
            )
        await self._catalog.refresh()  # validate against the live catalog, not a stale cache
        chat = await self._catalog.get(conversation_id)
        if _normalized_name(chat.name) != _normalized_name(expected_chat_name):
            raise InvalidRequest(
                "expected_chat_name does not match the chat's current name; "
                "check the recipient with list_chats."
            )
        receipt = await self._writer.send(chat, text)
        return SendResult(
            conversation_id=chat.id,
            chat_name=chat.name,
            message_id=receipt.message_id,
            sent_at=receipt.sent_at,
            characters=len(text),
        )
