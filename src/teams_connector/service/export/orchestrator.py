"""ExportRequest → ExportArtifact: the only export path (MCP and UI).

1. Fetch each chat's selection newest first into the archive (bounded by the latest-N limit).
2. Write each chat's archived selection oldest first, streaming from the archive in batches (retained
   messages the server no longer returns are included and deleted ones are labelled).
3. Package one file, or one ZIP for several.

The export runs inside the caller (an MCP tool call, or a UI background task that reports progress and
can be cancelled). A cancelled export leaves no partial file.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from teams_connector.domain.errors import InvalidRequest
from teams_connector.domain.models import Chat, ExportArtifact, ExportedChat, ExportProgress, ExportRequest
from teams_connector.service import localfiles
from teams_connector.service.catalog import Catalog
from teams_connector.service.export import formatter
from teams_connector.service.export.packaging import filename_stem, package
from teams_connector.service.history import History, check_range
from teams_connector.store.db import Store, iso

BATCH = 500

Progress = Callable[[ExportProgress], None]


class Exports:
    def __init__(self, store: Store, catalog: Catalog, history: History) -> None:
        self._store = store
        self._catalog = catalog
        self._history = history

    async def export(self, request: ExportRequest, *, progress: Progress | None = None) -> ExportArtifact:
        check_range(request.since, request.until)
        ids = list(dict.fromkeys(i.strip() for i in request.conversation_ids if i.strip()))
        if not ids:
            raise InvalidRequest("Select at least one conversation.")
        chats = [await self._catalog.get(i) for i in ids]
        state = ExportProgress(phase="retrieving", chats_total=len(chats))

        def report(**changes: object) -> None:
            for key, value in changes.items():
                setattr(state, key, value)
            if progress:
                progress(state.model_copy())

        complete: dict[str, bool] = {}
        base_pages = base_messages = 0
        for index, chat in enumerate(chats, start=1):
            report(chat_index=index)

            def on_page(
                pages: int, selected: int, base_p: int = base_pages, base_m: int = base_messages
            ) -> None:
                report(pages_fetched=base_p + pages, messages_retrieved=base_m + selected)

            fetch = await self._history.fetch_range(
                chat, since=request.since, until=request.until, limit=request.message_limit, on_page=on_page
            )
            complete[chat.id] = fetch.complete
            base_pages += fetch.pages
            base_messages += fetch.selected

        report(phase="writing", chat_index=0)
        exported_at = datetime.now(UTC)
        suffix = ".jsonl" if request.format == "jsonl" else ".txt"
        work = Path(tempfile.mkdtemp(prefix=".export-", dir=localfiles.kept_dir("exports")))
        try:
            outputs: list[Path] = []
            summary: list[ExportedChat] = []
            used: set[str] = set()
            combined = work / f"teams-conversations{suffix}" if request.combine and len(chats) > 1 else None
            for index, chat in enumerate(chats, start=1):
                report(chat_index=index)
                stem = filename_stem(chat)
                while stem.casefold() in used:
                    stem += "-2"
                used.add(stem.casefold())
                path = combined or work / f"{stem}{suffix}"
                written = self._write_chat(chat, path, request, exported_at, append=combined is not None,
                                           written_before=state.messages_written, report=report)  # fmt: skip
                summary.append(ExportedChat(conversation_id=chat.id, name=chat.name, messages=written,
                                            remote_complete=complete[chat.id]))  # fmt: skip
                if path not in outputs:
                    outputs.append(path)
            target, content_type = package(outputs, zip_name="teams-conversations.zip")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        report(phase="done")
        return ExportArtifact(
            path=str(target),
            filename=target.name,
            content_type=content_type,
            size=target.stat().st_size,
            message_count=sum(c.messages for c in summary),
            chats=summary,
        )

    def _write_chat(
        self,
        chat: Chat,
        path: Path,
        request: ExportRequest,
        exported_at: datetime,
        *,
        append: bool,
        written_before: int,
        report: Callable[..., None],
    ) -> int:
        total = self._store.count(chat.id, since=request.since, until=request.until)
        count = min(total, request.message_limit) if request.message_limit else total
        cutoff = (
            self._store.latest_cutoff(chat.id, since=request.since, until=request.until, limit=count)
            if request.message_limit and count
            else None
        )
        written = 0
        with path.open("a" if append else "w", encoding="utf-8", newline="\n") as output:
            if request.format == "txt":
                if append and output.tell():
                    output.write("\n")
                output.write(
                    formatter.txt_header(chat, messages=count, request=request, exported_at=exported_at)
                )
            after: tuple[str, str] | None = None
            while True:
                batch = self._store.chronological(
                    chat.id,
                    since=request.since,
                    until=request.until,
                    after=after,
                    from_key=cutoff,
                    limit=BATCH,
                )
                if not batch:
                    break
                for message in batch:
                    message = self._history.annotate(message)
                    if request.format == "txt":
                        output.write(formatter.txt_message(message))
                    else:
                        output.write(formatter.jsonl_record(chat, message))
                    written += 1
                last = batch[-1]
                after = (iso(last.sent_at) or "", last.id)
                report(messages_written=written_before + written)
        return written
