from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from teams_exporter.teams_connector import ChatSummary, TeamsConnector
from teams_exporter.teams_message_formatter import TeamsMessageFormatter


class TeamsExporter:
    """Coordinate message retrieval and write readable UTF-8 text exports."""

    def __init__(
        self,
        connector: TeamsConnector,
        formatter: TeamsMessageFormatter,
    ) -> None:
        self.connector = connector
        self.formatter = formatter

    def export_chat(
        self,
        chat: ChatSummary,
        destination: Path,
        *,
        limit: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Path:
        messages = self.connector.get_messages(
            chat.id,
            limit=limit,
            since=since,
            until=until,
        )
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"Conversation: {chat.display_name}",
            f"Conversation ID: {chat.id}",
            f"Exported: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
            f"Messages: {len(messages)}",
        ]
        if since is not None:
            lines.append(f"Since: {since.isoformat()}")
        if until is not None:
            lines.append(f"Until: {until.isoformat()}")
        if limit is not None:
            lines.append(f"Limit: {limit}")
        lines.append("")

        for index, message in enumerate(messages):
            if index:
                lines.append("")
            timestamp = self._message_datetime(message)
            timestamp_text = (
                timestamp.isoformat().replace("+00:00", "Z")
                if timestamp
                else "unknown time"
            )
            author = self._author(message)
            lines.append(f"[{timestamp_text}] {author}")
            body = self.formatter.format_message(message)
            lines.extend(body.splitlines() if body else ["[No text content]"])

        destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return destination

    def export_chats(
        self,
        chats: list[ChatSummary],
        destination: Path,
        *,
        limit: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Path]:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        exported: list[Path] = []
        used_names: set[str] = set()
        for chat in chats:
            base = self._filename_stem(chat)
            stem = base
            suffix = 2
            while stem.casefold() in used_names:
                stem = f"{base}-{suffix}"
                suffix += 1
            used_names.add(stem.casefold())
            exported.append(
                self.export_chat(
                    chat,
                    directory / f"{stem}.txt",
                    limit=limit,
                    since=since,
                    until=until,
                )
            )
        return exported

    @staticmethod
    def _author(message: dict[str, Any]) -> str:
        for key in (
            "imdisplayname",
            "imDisplayName",
            "fromDisplayNameInToken",
            "displayName",
            "author",
            "from",
        ):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "Unknown sender"

    @staticmethod
    def _message_datetime(message: dict[str, Any]) -> datetime | None:
        return TeamsConnector._message_datetime(message)

    @staticmethod
    def _filename_stem(chat: ChatSummary) -> str:
        label = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", chat.display_name)
        label = re.sub(r"\s+", " ", label).strip(" ._")[:70] or "chat"
        chat_id = re.sub(r"[^A-Za-z0-9]+", "-", chat.id).strip("-")[:32]
        return f"{label}-{chat_id}" if chat_id else label
