"""ExportRequest → ExportArtifact: the only export path (MCP and UI).

1. Fetch each chat's selection newest first into the archive (bounded by the latest-N limit).
2. Write each chat's archived selection oldest first, streaming from the archive in batches (retained
   messages the server no longer returns are included and deleted ones are labelled).
3. Package one file, or one ZIP for several.

The export runs inside the caller (an MCP tool call, or a UI background task that reports progress and
can be cancelled). A cancelled export leaves no partial file.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from teams_connector.domain.errors import ConnectorError, InvalidRequest
from teams_connector.domain.models import (
    Attachment,
    Chat,
    ExportArtifact,
    ExportedChat,
    ExportProgress,
    ExportRequest,
)
from teams_connector.service import localfiles
from teams_connector.service.catalog import Catalog
from teams_connector.service.export import formatter
from teams_connector.service.export.packaging import (
    filename_stem,
    package,
    package_conversation,
    unique_asset_name,
)
from teams_connector.service.files import Files
from teams_connector.service.history import History, check_range
from teams_connector.store.db import Store, iso

BATCH = 500
MAX_ASSETS_PER_CONVERSATION = 200
MAX_ASSET_BYTES = 100 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 250 * 1024 * 1024

Progress = Callable[[ExportProgress], None]


class Exports:
    def __init__(self, store: Store, catalog: Catalog, history: History, files: Files) -> None:
        self._store = store
        self._catalog = catalog
        self._history = history
        self._files = files

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

        exported_at = datetime.now(UTC)
        suffix = ".jsonl" if request.format == "jsonl" else ".txt"
        work = Path(tempfile.mkdtemp(prefix=".export-", dir=localfiles.kept_dir("exports")))
        try:
            outputs: list[Path] = []
            summary: list[ExportedChat] = []
            used: set[str] = set()
            attachments_total = attachments_downloaded = attachments_failed = 0
            report(phase="writing", chat_index=0)
            for index, chat in enumerate(chats, start=1):
                report(phase="writing", chat_index=index)
                stem = self._unique_stem(chat, used)
                text_path = work / f"{stem}{suffix}"
                written = self._write_chat(
                    chat,
                    text_path,
                    request,
                    exported_at,
                    written_before=state.messages_written,
                    report=report,
                )
                per_chat, total, downloaded, failed = await self._export_chat_assets(
                    chat,
                    request,
                    text_path,
                    stem,
                    work,
                    report,
                    attachments_total,
                    attachments_downloaded,
                    attachments_failed,
                )
                attachments_total += total
                attachments_downloaded += downloaded
                attachments_failed += failed
                outputs.append(per_chat)
                summary.append(
                    ExportedChat(
                        conversation_id=chat.id,
                        name=chat.name,
                        messages=written,
                        remote_complete=complete[chat.id],
                        attachments_downloaded=downloaded,
                        attachments_failed=failed,
                    )
                )
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

    @staticmethod
    def _unique_stem(chat: Chat, used: set[str]) -> str:
        stem = filename_stem(chat)
        candidate = stem
        index = 2
        while candidate.casefold() in used:
            candidate = f"{stem}-{index}"
            index += 1
        used.add(candidate.casefold())
        return candidate

    async def _export_chat_assets(
        self,
        chat: Chat,
        request: ExportRequest,
        text_path: Path,
        stem: str,
        work: Path,
        report: Callable[..., None],
        total_before: int,
        downloaded_before: int,
        failed_before: int,
    ) -> tuple[Path, int, int, int]:
        if not request.include_attachments:
            return text_path, 0, 0, 0
        references = self._selected_attachments(chat, request)
        if not references:
            return text_path, 0, 0, 0

        report(phase="attachments", attachments_total=total_before + len(references))
        replacements: dict[str, str] = {}
        files: list[tuple[Path, str]] = []
        names = {text_path.name.casefold()}
        downloaded = failed = total_bytes = 0
        for index, (reference, attachment) in enumerate(references.items(), start=1):
            marker = f"[Attachment unavailable: {attachment.name}]"
            remaining = MAX_TOTAL_ASSET_BYTES - total_bytes
            if index > MAX_ASSETS_PER_CONVERSATION or remaining <= 0:
                replacements[reference] = marker
                failed += 1
                report(
                    attachments_downloaded=downloaded_before + downloaded,
                    attachments_failed=failed_before + failed,
                )
                continue
            partial = work / f"asset-{index}.part"
            try:
                remote = await self._files.download_selected_attachment(
                    attachment,
                    partial,
                    max_bytes=min(MAX_ASSET_BYTES, remaining),
                )
                name = unique_asset_name(remote.name or attachment.name, names)
                final_path = work / f"asset-{index}.bin"
                partial.replace(final_path)
                files.append((final_path, name))
                replacements[reference] = name
                downloaded += 1
                total_bytes += remote.size
            except ConnectorError:
                replacements[reference] = marker
                failed += 1
            finally:
                partial.unlink(missing_ok=True)
            report(
                attachments_downloaded=downloaded_before + downloaded,
                attachments_failed=failed_before + failed,
            )

        self._rewrite_asset_references(text_path, references, replacements)
        result = package_conversation(
            text_path,
            files,
            folder_name=stem,
            has_attachments=bool(references),
        )
        return result, len(references), downloaded, failed

    def _selected_attachments(self, chat: Chat, request: ExportRequest) -> dict[str, Attachment]:
        total = self._store.count(chat.id, since=request.since, until=request.until)
        count = min(total, request.message_limit) if request.message_limit else total
        cutoff = (
            self._store.latest_cutoff(chat.id, since=request.since, until=request.until, limit=count)
            if request.message_limit and count
            else None
        )
        attachments: dict[str, Attachment] = {}
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
                for attachment in message.attachments:
                    if attachment.kind not in {"file", "image"}:
                        continue
                    key = attachment.url or self._attachment_marker(attachment)
                    attachments.setdefault(key, attachment)
            last = batch[-1]
            after = (iso(last.sent_at) or "", last.id)
        return attachments

    @staticmethod
    def _attachment_marker(attachment: Attachment) -> str:
        label = "Image" if attachment.kind == "image" else "Attachment"
        return f"[{label}: {attachment.name}]"

    @classmethod
    def _rewrite_asset_references(
        cls,
        path: Path,
        references: dict[str, Attachment],
        replacements: dict[str, str],
    ) -> None:
        text = path.read_text(encoding="utf-8")
        jsonl = path.suffix.casefold() == ".jsonl"
        for reference, attachment in sorted(references.items(), key=lambda item: len(item[0]), reverse=True):
            text = cls._replace_asset_reference(text, reference, replacements[reference], jsonl=jsonl)
            if not attachment.url:
                text = cls._replace_asset_reference(
                    text,
                    cls._attachment_marker(attachment),
                    replacements[reference],
                    jsonl=jsonl,
                )
        path.write_text(text, encoding="utf-8", newline="\n")

    @staticmethod
    def _replace_asset_reference(text: str, reference: str, replacement: str, *, jsonl: bool) -> str:
        if jsonl:
            reference = json.dumps(reference, ensure_ascii=False)[1:-1]
            replacement = json.dumps(replacement, ensure_ascii=False)[1:-1]
        return text.replace(reference, replacement)

    def _write_chat(
        self,
        chat: Chat,
        path: Path,
        request: ExportRequest,
        exported_at: datetime,
        *,
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
        with path.open("w", encoding="utf-8", newline="\n") as output:
            if request.format == "txt":
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
