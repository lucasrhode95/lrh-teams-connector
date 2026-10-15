"""ExportRequest → ExportArtifact: the only export path (MCP and UI).

1. Read each chat's selection from Teams page by page, newest first, and spool each page's records to the
   export's own temporary file (bounded by the latest-N limit).
2. Write each chat's file oldest first by copying the spooled pages in reverse. Attachments the messages
   reference are downloaded and written beside the text under their names.
3. Package one file, or one ZIP for several.

Nothing is kept outside the temporary folder. The export runs inside the caller (an MCP tool call, or a UI
background task that reports progress and can be cancelled). A cancelled export leaves no partial file.
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

MAX_ASSETS_PER_CONVERSATION = 200
MAX_ASSET_BYTES = 100 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 250 * 1024 * 1024

Progress = Callable[[ExportProgress], None]
Span = tuple[int, int, int]  # one spooled page: where it starts in the spool file, its length, its messages


class Exports:
    def __init__(self, catalog: Catalog, history: History, files: Files) -> None:
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

        exported_at = datetime.now(UTC)
        suffix = ".jsonl" if request.format == "jsonl" else ".txt"
        work = Path(tempfile.mkdtemp(prefix=".export-", dir=localfiles.kept_dir("exports")))
        try:
            outputs: list[Path] = []
            summary: list[ExportedChat] = []
            used: set[str] = set()
            attachments_total = attachments_downloaded = attachments_failed = 0
            for index, chat in enumerate(chats, start=1):
                report(phase="retrieving", chat_index=index)
                stem = self._unique_stem(chat, used)
                spool = work / f"{stem}.spool"
                spans, references, written = await self._spool(chat, request, spool, report, state)
                text_path = work / f"{stem}{suffix}"
                report(phase="writing", chat_index=index)
                self._assemble(chat, text_path, spool, spans, request, exported_at, written, report, state)
                spool.unlink(missing_ok=True)
                per_chat, total, downloaded, failed = await self._export_chat_assets(
                    references,
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
                        remote_complete=True,
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

    async def _spool(
        self,
        chat: Chat,
        request: ExportRequest,
        spool: Path,
        report: Callable[..., None],
        state: ExportProgress,
    ) -> tuple[list[Span], dict[str, Attachment], int]:
        """Read the selection from Teams, newest page first, and spool each page's records. Returns the page
        spans, the attachments the messages reference, and the message count."""
        spans: list[Span] = []
        references: dict[str, Attachment] = {}
        written = 0
        with spool.open("wb") as output:
            async for page in self._history.selection(
                chat, since=request.since, until=request.until, message_limit=request.message_limit
            ):
                start = output.tell()
                for message in page:
                    record = (
                        formatter.txt_message(message)
                        if request.format == "txt"
                        else formatter.jsonl_record(chat, message)
                    )
                    output.write(record.encode("utf-8"))
                    written += 1
                    for attachment in message.attachments:
                        if attachment.kind in {"file", "image"}:
                            key = attachment.url or self._attachment_marker(attachment)
                            references.setdefault(key, attachment)
                spans.append((start, output.tell() - start, len(page)))
                report(
                    pages_fetched=state.pages_fetched + 1,
                    messages_retrieved=state.messages_retrieved + len(page),
                )
        return spans, references, written

    def _assemble(
        self,
        chat: Chat,
        path: Path,
        spool: Path,
        spans: list[Span],
        request: ExportRequest,
        exported_at: datetime,
        count: int,
        report: Callable[..., None],
        state: ExportProgress,
    ) -> None:
        """Write the chat's file oldest first: the spooled pages copied in reverse, after the TXT header."""
        with path.open("w", encoding="utf-8", newline="\n") as output, spool.open("rb") as source:
            if request.format == "txt":
                output.write(
                    formatter.txt_header(chat, messages=count, request=request, exported_at=exported_at)
                )
            for start, length, messages in reversed(spans):
                source.seek(start)
                output.write(source.read(length).decode("utf-8"))
                report(messages_written=state.messages_written + messages)

    async def _export_chat_assets(
        self,
        references: dict[str, Attachment],
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
