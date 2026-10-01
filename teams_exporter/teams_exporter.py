from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable

from teams_exporter.teams_connector import (
    ChatSummary,
    ExtractionCancelled,
    TeamsConnector,
)
from teams_exporter.teams_message_formatter import TeamsMessageFormatter
from teams_exporter.teams_service import TeamsService


class TeamsExporter:
    """Coordinate message retrieval and write readable UTF-8 text exports."""

    def __init__(
        self,
        connector: TeamsConnector,
        formatter: TeamsMessageFormatter,
        service: TeamsService | None = None,
    ) -> None:
        self.connector = connector
        self.formatter = formatter
        self.service = service

    def export_chat(
        self,
        chat: ChatSummary,
        destination: Path,
        limit: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cancel_event: Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        """Create an atomic chronological export using a disk-backed page spool."""
        self._raise_if_cancelled(cancel_event)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        page_offsets: list[int] = []
        message_count = 0
        staging_path: Path | None = None

        with tempfile.SpooledTemporaryFile(
            max_size=1024 * 1024,
            mode="w+t",
            encoding="utf-8",
            newline="\n",
        ) as spool:
            def report_fetch(pages: int, messages: int, retries: int) -> None:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "phase": "retrieving",
                            "pages_fetched": pages,
                            "messages_retrieved": messages,
                            "messages_written": 0,
                            "retry_count": retries,
                        }
                    )

            if self.service is not None:
                self.service.fetch_chat_to_archive(
                    chat.id,
                    since=since,
                    until=until,
                    cancel_event=cancel_event,
                    progress_callback=report_fetch,
                )
                message_count, cutoff = self.service.archived_export_message_count(
                    chat.id,
                    since=since,
                    until=until,
                    limit=limit,
                )
                pages = self.service.iter_archived_export_pages(
                    chat.id,
                    since=since,
                    until=until,
                    from_key=cutoff,
                )
            else:
                pages = self.connector.iter_message_pages(
                    chat.id,
                    limit=limit,
                    since=since,
                    until=until,
                    cancel_event=cancel_event,
                    progress_callback=report_fetch,
                )
            for page in pages:
                page_offsets.append(spool.tell())
                spool.write(json.dumps(page, ensure_ascii=False, separators=(",", ":")))
                spool.write("\n")
                if self.service is None:
                    message_count += len(page)

            coverage = self.connector.last_message_read_coverage
            missing_timestamps = int(
                coverage.get("missing_timestamp_messages_excluded", 0)
            )
            retry_count = int(coverage.get("retry_count", 0))
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            staging_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                    lines = [
                        f"Conversation: {chat.display_name}",
                        f"Conversation ID: {chat.id}",
                        f"Exported: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
                        f"Messages: {message_count}",
                    ]
                    if since is not None:
                        lines.append(f"Since: {since.isoformat()}")
                    if until is not None:
                        lines.append(f"Until: {until.isoformat()}")
                    if limit is not None:
                        lines.append(f"Limit: {limit}")
                    if missing_timestamps:
                        lines.append(
                            "Messages excluded because their timestamp was missing: "
                            f"{missing_timestamps}"
                        )
                    if retry_count:
                        lines.append(f"Transient read retries: {retry_count}")
                    output.write("\n".join(lines) + "\n\n")

                    written = 0
                    replay_offsets = (
                        page_offsets
                        if self.service is not None
                        else reversed(page_offsets)
                    )
                    for offset in replay_offsets:
                        self._raise_if_cancelled(cancel_event)
                        spool.seek(offset)
                        page = json.loads(spool.readline())
                        for message in page:
                            self._raise_if_cancelled(cancel_event)
                            if written:
                                output.write("\n")
                            timestamp = TeamsConnector.message_datetime(message)
                            timestamp_text = (
                                timestamp.isoformat().replace("+00:00", "Z")
                                if timestamp
                                else "unknown time"
                            )
                            marker = (
                                " "
                                + self.formatter.soft_deleted_marker(
                                    message.get("__deleted_at")
                                )
                                if message.get("__is_deleted")
                                else ""
                            )
                            output.write(
                                f"[{timestamp_text}] {self._author(message)}{marker}\n"
                            )
                            body = message.get("__formatted_body")
                            if isinstance(body, str):
                                body = self.formatter.sanitize_preformatted_body(body)
                            else:
                                body = self.formatter.format_message(message)
                            output.write(body if body else "[No text content]")
                            output.write("\n")
                            written += 1
                            if progress_callback is not None and (
                                written == 1 or written % 25 == 0 or written == message_count
                            ):
                                progress_callback(
                                    {
                                        "phase": "formatting",
                                        "pages_fetched": len(page_offsets),
                                        "messages_retrieved": message_count,
                                        "messages_written": written,
                                        "retry_count": retry_count,
                                    }
                                )

                self._raise_if_cancelled(cancel_event)
                os.replace(staging_path, destination)
                staging_path = None
                return destination
            finally:
                if staging_path is not None:
                    staging_path.unlink(missing_ok=True)

    @staticmethod
    def _raise_if_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("Extraction was cancelled by the caller.")

    def export_chats(
        self,
        chats: list[ChatSummary],
        destination: Path,
        limit: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cancel_event: Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[Path]:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        exported: list[Path] = []
        used_names: set[str] = set()
        for index, chat in enumerate(chats, start=1):
            self._raise_if_cancelled(cancel_event)
            base = self.filename_stem(chat)
            stem = base
            suffix = 2
            while stem.casefold() in used_names:
                stem = f"{base}-{suffix}"
                suffix += 1
            used_names.add(stem.casefold())

            def report_chat_progress(update: dict[str, Any]) -> None:
                if progress_callback is not None:
                    progress_callback(
                        {
                            **update,
                            "chat_index": index,
                            "chats_total": len(chats),
                        }
                    )

            exported.append(
                self.export_chat(
                    chat,
                    directory / f"{stem}.txt",
                    limit=limit,
                    since=since,
                    until=until,
                    cancel_event=cancel_event,
                    progress_callback=report_chat_progress,
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
    def filename_stem(chat: ChatSummary) -> str:
        label = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", chat.display_name)
        label = re.sub(r"\s+", " ", label).strip(" ._")[:70] or "chat"
        chat_id = re.sub(r"[^A-Za-z0-9]+", "-", chat.id).strip("-")[:32]
        return f"{label}-{chat_id}" if chat_id else label
