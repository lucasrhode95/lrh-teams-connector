"""ExportRequest → ExportArtifact: the only export path (MCP and UI).

1. Read each chat's selection from Teams page by page, newest first, and spool each page's records to the
   export's own temporary file (bounded by the latest-N limit). Each downloadable attachment gets its final
   name here, so the text already names the file written beside it. Meeting notes and the AI summaries of
   transcripts are read here too (notes once per page link), and written as text after their markers.
2. Write each chat's file oldest first by copying the spooled pages in reverse. The attachments are fetched
   through the download cache and written beside the text under those names; one that cannot be fetched is
   marked in the text instead.
3. Package one flat file, or one ZIP with every flat file at its root and its attachments in a folder
   named after it.

Nothing is kept outside the temporary folder and the download cache. The export runs inside the caller (an MCP
tool call, or a UI background task that reports progress and can be cancelled). A cancelled export leaves no
partial file.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    SavedFile,
)
from teams_connector.service import localfiles
from teams_connector.service.catalog import Catalog
from teams_connector.service.export import formatter
from teams_connector.service.export.packaging import (
    ConversationOutput,
    filename_stem,
    package,
    unique_asset_name,
)
from teams_connector.service.files import Files
from teams_connector.service.history import History, check_range
from teams_connector.service.summaries import MeetingSummaries

MAX_ASSETS_PER_CONVERSATION = 200
MAX_ASSET_BYTES = 100 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 250 * 1024 * 1024
DOWNLOAD_CONCURRENCY = 4  # attachments fetched at once, as many as the transport sends at once

Progress = Callable[[ExportProgress], None]
Span = tuple[int, int, int]  # one spooled page: where it starts in the spool file, its length, its messages


class Exports:
    def __init__(self, catalog: Catalog, history: History, files: Files, summaries: MeetingSummaries) -> None:
        self._catalog = catalog
        self._history = history
        self._files = files
        self._summaries = summaries

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
            outputs: list[ConversationOutput] = []
            summary: list[ExportedChat] = []
            used: set[str] = set()
            attachments_total = attachments_downloaded = attachments_failed = 0
            for index, chat in enumerate(chats, start=1):
                report(phase="retrieving", chat_index=index)
                stem = self._unique_stem(chat, used)
                spool = work / f"{stem}.spool"
                text_name = f"{stem}{suffix}"
                spans, references, written, notes = await self._spool(
                    chat, request, spool, report, state, text_name
                )
                text_path = work / f"{stem}{suffix}"
                report(phase="writing", chat_index=index)
                self._assemble(chat, text_path, spool, spans, request, exported_at, written, report, state)
                spool.unlink(missing_ok=True)
                read_notes = [text for text in notes.values() if text is not None]
                per_chat, total, downloaded, failed = await self._export_chat_assets(
                    references,
                    request,
                    text_path,
                    report,
                    attachments_total,
                    attachments_downloaded,
                    attachments_failed,
                    # the notes already read share the conversation's attachment budget
                    budget_used=(len(notes), sum(len(text.encode("utf-8")) for text in read_notes)),
                )
                downloaded += len(read_notes)
                failed += len(notes) - len(read_notes)
                attachments_total += total + len(notes)
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
    ) -> tuple[list[Span], dict[str, Attachment], int, dict[str, str | None]]:
        """Read the selection from Teams, newest page first, and spool each page's records. Returns the page
        spans, the attachments the messages reference by their final names, the message count, and the
        meeting-notes pages by link, each with its text or None when it was not read. The chat's own file
        name is taken first, so no attachment can replace it in the ZIP."""
        spans: list[Span] = []
        references: dict[str, Attachment] = {}
        names: dict[tuple[str, str], str] = {}
        used: set[str] = {text_name.casefold()}
        notes: dict[str, str | None] = {}  # page link -> the notes as text, or None when unreadable
        written = 0
        with spool.open("wb") as output:
            async for page in self._history.selection(
                chat, since=request.since, until=request.until, message_limit=request.message_limit
            ):
                start = output.tell()
                for message in page:
                    if request.include_attachments:
                        await self._add_meeting_notes(message, notes)
                    if request.include_summaries:
                        await self._summaries.add_to_message(
                            message, include_transcripts=request.include_transcripts
                        )
                    _name_attachments(
                        message,
                        names,
                        used,
                        references,
                        include_files=request.include_attachments,
                        include_transcripts=request.include_transcripts,
                    )
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
        return spans, references, written, notes

    async def _add_meeting_notes(self, message: Message, notes: dict[str, str | None]) -> None:
        """Put each meeting-notes page the message links to after its marker, read once per export; a page
        that cannot be read, or lies beyond the conversation's attachment limits (count and total size), is
        marked unavailable, as an attachment would be."""
        for item in message.attachments:
            if item.source != "loop" or not item.url:
                continue
            if item.url not in notes:
                read = sum(len(text.encode("utf-8")) for text in notes.values() if text)
                notes[item.url] = None
                if len(notes) <= MAX_ASSETS_PER_CONVERSATION and read < MAX_TOTAL_ASSET_BYTES:
                    with contextlib.suppress(ConnectorError):  # an unreadable page stays None
                        notes[item.url] = await self._files.meeting_notes(item.url)
            marker = f"[Meeting notes] {item.url}"
            document = notes[item.url]
            replacement = (
                f"[Attachment unavailable: {item.name}] {item.url}"
                if document is None
                else f"{marker}\n{document}"
            )
            message.text = message.text.replace(marker, replacement)

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
        report: Callable[..., None],
        total_before: int,
        downloaded_before: int,
        failed_before: int,
        *,
        budget_used: tuple[int, int] = (0, 0),
    ) -> tuple[ConversationOutput, int, int, int]:
        """Fetch the chat's attachments through the download cache, ``DOWNLOAD_CONCURRENCY`` at a time, to be
        packaged beside its text.

        An attachment that cannot be fetched, or lies beyond the export's limits, is marked in the text. The
        limits are applied in the order the text names the attachments, so the result does not depend on
        which download finishes first; a file fetched in parallel that no longer fits the total is dropped.
        ``budget_used`` is the (items, bytes) of the conversation's limits the meeting notes already took.
        """
        if not references:  # only the kinds the request includes are referenced
            return ConversationOutput(text_path), 0, 0, 0

        report(phase="attachments", attachments_total=total_before + len(references))
        items_before, bytes_before = budget_used
        allowed = list(references.items())[: max(0, MAX_ASSETS_PER_CONVERSATION - items_before)]
        max_bytes = min(MAX_ASSET_BYTES, MAX_TOTAL_ASSET_BYTES - bytes_before)
        results: dict[str, SavedFile | None] = {}
        gate = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

        async def fetch(name: str, attachment: Attachment) -> None:
            async with gate:
                try:
                    results[name] = await self._files.save(attachment, max_bytes=max_bytes)
                except ConnectorError:
                    results[name] = None
            done = [saved for saved in results.values() if saved is not None]
            report(
                attachments_downloaded=downloaded_before + len(done),
                attachments_failed=failed_before + len(results) - len(done),
            )

        if max_bytes > 0:
            async with asyncio.TaskGroup() as group:
                for name, attachment in allowed:
                    group.create_task(fetch(name, attachment))

        # In the order the text names them: the first files that fit the conversation's budget are kept.
        files: list[tuple[Path, str]] = []
        unavailable: list[str] = []
        total_bytes = bytes_before
        for name in references:
            saved = results.get(name)
            if saved is None or total_bytes + saved.size > MAX_TOTAL_ASSET_BYTES:
                unavailable.append(name)
                continue
            files.append((Path(saved.path), name))
            total_bytes += saved.size
        report(
            attachments_downloaded=downloaded_before + len(files),
            attachments_failed=failed_before + len(unavailable),
        )
        _mark_unavailable(text_path, unavailable)
        return ConversationOutput(text_path, files), len(references), len(files), len(unavailable)


TRANSCRIPT_FOLDER = "transcripts/"


def _name_attachments(
    message: Message,
    names: dict[tuple[str, str], str],
    used: set[str],
    references: dict[str, Attachment],
    *,
    include_files: bool,
    include_transcripts: bool,
) -> None:
    """Give each downloadable attachment one name for this export. A source keeps its name wherever it
    appears; a different source whose name is taken gets a number. The marker is renamed to match.

    Files and images are named in the conversation's folder; a transcript goes to the ``transcripts/`` folder
    as ``<title>.txt``. An attachment the request leaves out is not referenced: its marker is removed for a
    transcript (it has no other form) and kept for a file, as an unsaved file always is.
    """
    kept: list[Attachment] = []
    for attachment in message.attachments:
        if attachment.source is None or attachment.source == "loop":  # notes go in the text instead
            kept.append(attachment)
            continue
        if attachment.source == "transcript":
            if not include_transcripts:
                message.text = _without(message.text, f"[Transcript: {attachment.name}]")
                continue
            kept.append(_name_transcript(attachment, names, used, references, message))
            continue
        if not include_files:
            kept.append(attachment)
            continue
        key = (attachment.source, attachment.source_id or attachment.url or attachment.name)
        final = names.get(key)
        if final is None:
            final = unique_asset_name(attachment.name, used)
            names[key] = final
            references[final] = attachment  # cached under its own name; packaged as ``final``
        if final != attachment.name:
            label = _label(attachment)
            message.text = message.text.replace(f"[{label}: {attachment.name}]", f"[{label}: {final}]")
            kept.append(attachment.model_copy(update={"name": final}))
        else:
            kept.append(attachment)
    message.attachments = kept


def _name_transcript(
    attachment: Attachment,
    names: dict[tuple[str, str], str],
    used: set[str],
    references: dict[str, Attachment],
    message: Message,
) -> Attachment:
    """A transcript's file in the export: ``transcripts/<title>.txt``, numbered when another name took it. The
    marker shows the file's name without its extension, so the marker and the file agree."""
    key = ("transcript", attachment.source_id or attachment.name)
    final = names.get(key)
    if final is None:
        final = TRANSCRIPT_FOLDER + unique_asset_name(f"{attachment.name}.txt", used)
        names[key] = final
        references[final] = attachment
    stem = Path(final).stem
    if stem != attachment.name:
        message.text = message.text.replace(f"[Transcript: {attachment.name}]", f"[Transcript: {stem}]")
    return attachment.model_copy(update={"name": stem})


def _without(text: str, marker: str) -> str:
    """The text with one marker line removed."""
    return "\n".join(line for line in text.split("\n") if line.strip() != marker).strip("\n")


def _label(attachment: Attachment) -> str:
    if attachment.source == "transcript":
        return "Transcript"
    return "Image" if attachment.kind == "image" else "File"


def _mark_unavailable(path: Path, names: list[str]) -> None:
    """Replace the markers of attachments that could not be included with an unavailable note. A transcript is
    named by its file's stem in the marker, a file by its name."""
    if not names:
        return
    text = path.read_text(encoding="utf-8")
    jsonl = path.suffix.casefold() == ".jsonl"
    for name in names:
        if name.startswith(TRANSCRIPT_FOLDER):
            stem = Path(name).stem
            old, new = f"[Transcript: {stem}]", f"[Attachment unavailable: {stem}]"
            text = _replace_text(text, old, new, jsonl=jsonl)
            continue
        for label in ("Image", "File"):
            old, new = f"[{label}: {name}]", f"[Attachment unavailable: {name}]"
            text = _replace_text(text, old, new, jsonl=jsonl)
    path.write_text(text, encoding="utf-8", newline="\n")


def _replace_text(text: str, old: str, new: str, *, jsonl: bool) -> str:
    if jsonl:
        old = json.dumps(old, ensure_ascii=False)[1:-1]
        new = json.dumps(new, ensure_ascii=False)[1:-1]
    return text.replace(old, new)
