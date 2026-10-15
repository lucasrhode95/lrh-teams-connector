"""Sending a message: validated against the live catalog, attempted exactly once.

The caller (MCP client) is responsible for the user's explicit authorization of the recipient and the
content. The send checks the recipient: ``expected_chat_name`` must match the chat's current name.

The body is either plain text, escaped and sent as paragraphs, or HTML, checked for active and unfinished
markup and sent as written.
"""

from __future__ import annotations

from teams_connector.domain.errors import InvalidRequest
from teams_connector.domain.models import SendResult
from teams_connector.remote.ports import ChatWriter
from teams_connector.service.catalog import Catalog
from teams_connector.service.passive_html import check_passive_html

MAX_TEXT_CHARS = 10_000


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


class Sender:
    def __init__(self, catalog: Catalog, writer: ChatWriter) -> None:
        self._catalog = catalog
        self._writer = writer

    async def send(
        self,
        conversation_id: str,
        *,
        expected_chat_name: str,
        text: str | None = None,
        html: str | None = None,
    ) -> SendResult:
        """Entry point: one message, as plain ``text`` or as ``html``. Exactly one of the two is required."""
        if (text is None) == (html is None):
            raise InvalidRequest("Pass exactly one of text (plain text) or html (HTML, sent as written).")
        if text is not None:
            _check_text(text)
        else:
            _check_html(html or "")
        if not expected_chat_name or not expected_chat_name.strip():
            raise InvalidRequest("expected_chat_name must be the chat's current name from list_chats.")
        chat = await self._catalog.recipient(conversation_id)  # the live name of this recipient only
        if _normalized_name(chat.name) != _normalized_name(expected_chat_name):
            raise InvalidRequest(
                "expected_chat_name does not match the chat's current name; "
                "check the recipient with list_chats."
            )
        content = text if text is not None else (html or "")
        receipt = await self._writer.send(chat, content, as_html=text is None)
        return SendResult(
            conversation_id=chat.id,
            chat_name=chat.name,
            message_id=receipt.message_id,
            sent_at=receipt.sent_at,
            characters=len(content),
        )
