"""Export rendering: TXT for people, JSONL (one record per message) for agents. Times are UTC."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from teams_connector.domain.models import Chat, ExportRequest, Message


def timestamp(value: datetime | None) -> str:
    if value is None:
        return "unknown time"
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def deleted_marker(deleted_at: datetime | None) -> str:
    """A prominent label: retained text of a deleted message must never read as a live message."""
    return f"[Soft deleted at {timestamp(deleted_at)}]" if deleted_at else "[Soft deleted]"


def txt_header(chat: Chat, *, messages: int, request: ExportRequest, exported_at: datetime) -> str:
    lines = [
        f"Conversation: {chat.name}",
        f"Conversation ID: {chat.id}",
        f"Exported: {timestamp(exported_at)}",
        "Time zone: UTC",
        f"Messages: {messages}",
    ]
    if request.since:
        lines.append(f"Since: {timestamp(request.since)}")
    if request.until:
        lines.append(f"Until: {timestamp(request.until)}")
    if request.message_limit:
        lines.append(f"Limit: latest {request.message_limit}")
    return "\n".join(lines) + "\n\n"


def txt_message(message: Message) -> str:
    head = f"[{timestamp(message.sent_at)}] {message.author or 'Unknown sender'}"
    if message.reply_to_id and message.reply_to_id != message.id:
        head += " (reply)"
    if message.is_deleted:
        head += " " + deleted_marker(message.deleted_at)
    body = message.text or "[No text content]"
    if message.subject:
        body = f"Subject: {message.subject}\n{body}"
    return f"{head}\n{body}\n\n"


def jsonl_record(chat: Chat, message: Message) -> str:
    record = {
        "conversation_id": chat.id,
        "chat_name": chat.name,
        "id": message.id,
        "sent_at": timestamp(message.sent_at) if message.sent_at else None,
        "author": message.author,
        "author_id": message.author_id,
        "is_self": message.is_self,
        "subject": message.subject,
        "reply_to_id": message.reply_to_id,
        "system_event": message.system_event,
        "text": message.text,
        "edited_at": timestamp(message.edited_at) if message.edited_at else None,
        "is_deleted": message.is_deleted,
        "deleted_at": timestamp(message.deleted_at) if message.deleted_at else None,
        "attachments": [a.model_dump(mode="json") for a in message.attachments],
    }
    return json.dumps({k: v for k, v in record.items() if v not in (None, [])}, ensure_ascii=False) + "\n"
