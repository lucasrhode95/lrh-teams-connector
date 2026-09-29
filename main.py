from datetime import datetime, timezone

from teams_exporter.teams_connector import TeamsConnector

connector = TeamsConnector()
connector.connect()

print("\nConnected.")

conversations = connector.list_chats(limit=15)

print(f"\nLatest {len(conversations)} chats (thread version order):")

for number, conversation in enumerate(conversations, start=1):
    last_message = conversation.get("lastMessage") or {}
    thread = conversation.get("threadProperties") or {}

    last_sender = (
            last_message.get("imdisplayname")
            or last_message.get("fromDisplayNameInToken")
            or "unknown"
    )

    message_timestamp = connector._conversation_activity_timestamp(
        {"lastMessage": last_message}
    )
    last_message_time = (
        datetime.fromtimestamp(
            message_timestamp,
            tz=timezone.utc,
        ).isoformat()
        if message_timestamp
        else "unknown"
    )

    conversation_type = (
            thread.get("productThreadType")
            or thread.get("threadType")
            or "unknown"
    )

    print(
        f"{number:>2}. {conversation.get('display_name')}\n"
        f"    type: {conversation_type}\n"
        f"    id: {conversation.get('id')}\n"
        f"    last sender: {last_sender}\n"
        f"    thread version: {conversation.get('version', 'unknown')}\n"
        f"    last message: {last_message_time}"
    )
