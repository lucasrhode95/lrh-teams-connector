import re
from datetime import datetime

from teams_exporter.teams_connector import ChatSummary, TeamsConnector
from teams_exporter.teams_message_formatter import TeamsMessageFormatter


CHAT_LIST_LIMIT = 15
MESSAGE_LIST_LIMIT = 20


def _timestamp_text(value: datetime | None) -> str:
    if value is None:
        return "unknown time"
    return value.isoformat().replace("+00:00", "Z")


def _message_author(message: dict[str, object], body: str) -> str:
    for key in (
        "imdisplayname",
        "imDisplayName",
        "fromDisplayNameInToken",
        "displayName",
    ):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # The `from` field can be a service URL for system events, not a person name.
    if body.lstrip().startswith(("<", "{")):
        return "System"
    return "Unknown sender"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _print_chat_list(chats: list[ChatSummary]) -> None:
    shown = chats[:CHAT_LIST_LIMIT]
    print(f"Latest {len(shown)} of {len(chats)} chats:")
    for number, chat in enumerate(shown, start=1):
        chat_type = chat.chat_type or "chat"
        print(
            f"{number:>2}. [{_timestamp_text(chat.last_message_at)}] "
            f"{chat.display_name} ({chat_type})"
        )


connector = TeamsConnector()
connector.login()

chats = connector.list_chats()
_print_chat_list(chats)

if not chats:
    print("No chats were returned by the CSA catalog.")
else:
    chat = chats[0]
    messages = connector.get_messages(chat.id, limit=MESSAGE_LIST_LIMIT)
    formatter = TeamsMessageFormatter()

    print(
        f"\nLatest {len(messages)} messages in {chat.display_name} "
        "(oldest to newest):"
    )

    for message in messages:
        body = _one_line(
            formatter.format_message(message) or "[No text content]"
        )
        author = _message_author(message, body)
        timestamp_text = _timestamp_text(connector._message_datetime(message))
        print(f"[{timestamp_text}] {author}: {body}")
