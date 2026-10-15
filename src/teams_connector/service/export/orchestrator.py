"""ExportRequest → ExportArtifact: the only export path (MCP and UI).

1. Read each chat's selection from Teams page by page, newest first, and spool each page's records to the
   export's own temporary file (bounded by the latest-N limit). Each downloadable attachment gets its final
   name here, so the text already names the file written beside it.
2. Write each chat's file oldest first by copying the spooled pages in reverse. The attachments are fetched
   through the download cache and written beside the text under those names; one that cannot be fetched is
   marked in the text instead.
3. Package one file, or one ZIP for several.

Nothing is kept outside the temporary folder and the download cache. The export runs inside the caller (an MCP
tool call, or a UI background task that reports progress and can be cancelled). A cancelled export leaves no
partial file.
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
    Message,
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
                text_name = f"{stem}{suffix}"
                spans, references, written = await self._spool(chat, request, spool, report, state, text_name)
                text_path = work / f"{stem}{suffix}"
                report(phase="writing", chat_index=index)
                self._assemble(chat, text_path, spool, spans, request, exported_at, written, report, state)
                spool.unlink(missing_ok=True)
                per_chat, total, downloaded, failed = await self._export_chat_assets(
                    references,
                    request,
                    text_path,
                    stem,
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
        text_name: str,
    ) -> tuple[list[Span], dict[str, Attachment], int]:
        """Read the selection from Teams, newest page first, and spool each page's records. Returns the page
        spans, the attachments the messages reference by their final names, and the message count. The chat's
        own file name is taken first, so no attachment can replace it in the ZIP."""
        spans: list[Span] = []
        references: dict[str, Attachment] = {}
        names: dict[tuple[str, str], str] = {}
        used: set[str] = {text_name.casefold()}
        written = 0
        with spool.open("wb") as output:
            async for page in self._history.selection(
                chat, since=request.since, until=request.until, message_limit=request.message_limit
            ):
                start = output.tell()
                for message in page:
                    _name_attachments(message, names, used, references)
                    record = (
                        formatter.txt_message(message)
                        if request.format == "txt"
                        else formatter.jsonl_record(chat, message)
                    )
                    output.write(record.encode("utf-8"))
                    written += 1
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
        report: Callable[..., None],
        total_before: int,
        downloaded_before: int,
        failed_before: int,
    ) -> tuple[Path, int, int, int]:
        """Fetch the chat's attachments through the download cache and package them with its text.

        An attachment that cannot be fetched, or lies beyond the export's limits, is marked in the text.
        """
        if not request.include_attachments or not references:
            return text_path, 0, 0, 0

        report(phase="attachments", attachments_total=total_before + len(references))
        files: list[tuple[Path, str]] = []
        unavailable: list[str] = []
        downloaded = failed = total_bytes = 0
        for index, (name, attachment) in enumerate(references.items(), start=1):
            remaining = MAX_TOTAL_ASSET_BYTES - total_bytes
            if index > MAX_ASSETS_PER_CONVERSATION or remaining <= 0:
                unavailable.append(name)
                failed += 1
            else:
                try:
                    saved = await self._files.save(attachment, max_bytes=min(MAX_ASSET_BYTES, remaining))
                except ConnectorError:
                    unavailable.append(name)
                    failed += 1
                else:
                    if saved.size > remaining:
                        unavailable.append(name)
                        failed += 1
                    else:
                        files.append((Path(saved.path), name))
                        downloaded += 1
                        total_bytes += saved.size
            report(
                attachments_downloaded=downloaded_before + downloaded,
                attachments_failed=failed_before + failed,
            )

        _mark_unavailable(text_path, unavailable)
        result = package_conversation(
            text_path,
            files,
            folder_name=stem,
            has_attachments=bool(references),
        )
        return result, len(references), downloaded, failed


def _name_attachments(
    message: Message,
    names: dict[tuple[str, str], str],
    used: set[str],
    references: dict[str, Attachment],
) -> None:
    """Give each downloadable attachment one name for this export. A source keeps its name wherever it
    appears; a different source whose name is taken gets a number. The marker is renamed to match."""
    for index, attachment in enumerate(message.attachments):
        if attachment.source is None:
            continue
        key = (attachment.source, attachment.source_id or attachment.url or attachment.name)
        final = names.get(key)
        if final is None:
            final = unique_asset_name(attachment.name, used)
            names[key] = final
            references[final] = attachment.model_copy(update={"name": final})
        if final != attachment.name:
            label = _label(attachment)
            message.text = message.text.replace(f"[{label}: {attachment.name}]", f"[{label}: {final}]")
            message.attachments[index] = attachment.model_copy(update={"name": final})


def _label(attachment: Attachment) -> str:
    return "Image" if attachment.kind == "image" else "File"


def _mark_unavailable(path: Path, names: list[str]) -> None:
    """Replace the markers of attachments that could not be included with an unavailable note."""
    if not names:
        return
    text = path.read_text(encoding="utf-8")
    jsonl = path.suffix.casefold() == ".jsonl"
    for name in names:
        for label in ("Image", "File"):
            text = _replace_text(text, f"[{label}: {name}]", f"[Attachment unavailable: {name}]", jsonl=jsonl)
    path.write_text(text, encoding="utf-8", newline="\n")


def _replace_text(text: str, old: str, new: str, *, jsonl: bool) -> str:
    if jsonl:
        old = json.dumps(old, ensure_ascii=False)[1:-1]
        new = json.dumps(new, ensure_ascii=False)[1:-1]
    return text.replace(old, new)
