"""Attachment downloads (MCP and UI) and profile photos.

Only attachments of messages Teams returns can be downloaded: no arbitrary URL is ever fetched. A download
reads its message from Teams again, by id or by its arrival time, and saves the files it names under
``<data directory>/downloads/<source>/<key>/``. The disk is the cache: a file already there is returned
without fetching its content. Files are removed after a week. Meeting notes are the exception: a Loop page
is live, so it is read again every time and saved as text that says when it was read.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from teams_connector.domain.errors import AuthenticationRequired, ConnectorError, InvalidRequest, NotFound
from teams_connector.domain.models import Attachment, DownloadedFiles, SavedFile, UnavailableFile
from teams_connector.remote.ports import FileReader, ImageReader, MeetingNotesReader, People, RemoteFile
from teams_connector.service import localfiles
from teams_connector.service.history import History

PHOTO_TTL_SECONDS = 3600
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_filename(name: str, fallback: str = "file") -> str:
    cleaned = _UNSAFE.sub("_", name).strip(" .")
    return cleaned[:150] or fallback


class Files:
    """A message's attachments, each fetched from its own source and kept in the download cache."""

    def __init__(
        self,
        history: History,
        sharepoint: FileReader,
        ams: ImageReader,
        hosted: ImageReader,
        notes: MeetingNotesReader,
    ) -> None:
        self._history = history
        self._sharepoint = sharepoint
        self._ams = ams
        self._hosted = hosted
        self._notes = notes

    async def download_attachments(
        self,
        conversation_id: str,
        *,
        message_id: str | None,
        sent_at: datetime | None,
        names: list[str] | None,
    ) -> DownloadedFiles:
        """Entry point: the files one message names (all of them, or those in ``names``), saved locally.

        The caller passes exactly one of ``message_id`` or ``sent_at``, validated at the surface.
        """
        message = await self._history.resolve(conversation_id, message_id=message_id, sent_at=sent_at)
        chosen = [item for item in message.attachments if item.source is not None]
        if names is not None:
            available = {item.name for item in chosen}
            pages = {item.name for item in message.attachments if item.kind == "link"}
            missing = [name for name in names if name not in available]
            if missing:
                listed = ", ".join(repr(name) for name in missing)
                hint = " SharePoint pages are links: open them instead." if set(missing) & pages else ""
                raise NotFound(f"The message has no downloadable attachment named {listed}.{hint}")
            chosen = [item for item in chosen if item.name in set(names)]
        if not chosen:
            raise NotFound("The message has no downloadable attachment.")
        saved: list[SavedFile] = []
        unavailable: list[UnavailableFile] = []
        first_error: ConnectorError | None = None
        for item in chosen:
            try:
                saved.append(await self.save(item, max_bytes=MAX_DOWNLOAD_BYTES))
            except AuthenticationRequired:
                raise
            except ConnectorError as exc:  # the other files are still saved; this one is reported
                first_error = first_error or exc
                unavailable.append(UnavailableFile(name=item.name, reason=str(exc)))
        if not saved and first_error is not None:
            raise first_error
        return DownloadedFiles(files=saved, unavailable=unavailable)

    async def save(self, attachment: Attachment, *, max_bytes: int) -> SavedFile:
        """Fetch one attachment into the cache, or return the cached file. Only its own source is read."""
        name = safe_filename(attachment.name)
        if attachment.source == "data" and attachment.payload is not None:
            payload = attachment.payload
            target = _cache_folder("data", attachment.source_id or "") / name
            if not target.is_file():
                await _fetch(target, lambda partial: _write_payload(partial, payload))
            return _saved(target, attachment.content_type)
        if attachment.source == "sharepoint" and attachment.url:
            url = attachment.url
            shared = await self._sharepoint.describe(url)
            target = _cache_folder("sharepoint", _digest(shared.item_id, shared.version)) / name
            if target.is_file():
                return _saved(target, attachment.content_type)
            remote = await _fetch(
                target, lambda partial: self._sharepoint.download(url, partial, max_bytes=max_bytes)
            )
            return _saved(target, remote.content_type)
        if attachment.source == "ams" and attachment.url and attachment.source_id:
            url = attachment.url
            target = _cache_folder("ams", attachment.source_id) / name
            if target.is_file():
                return _saved(target, attachment.content_type)
            remote = await _fetch(
                target, lambda partial: self._ams.download(url, partial, max_bytes=max_bytes)
            )
            return _saved(target, remote.content_type)
        if attachment.source == "graph" and attachment.url and attachment.source_id:
            url = attachment.url
            target = _cache_folder("graph", attachment.source_id) / name
            if target.is_file():
                return _saved(target, attachment.content_type)
            remote = await _fetch(
                target, lambda partial: self._hosted.download(url, partial, max_bytes=max_bytes)
            )
            return _saved(target, remote.content_type)
        if attachment.source == "loop" and attachment.url:
            document = await self.meeting_notes(attachment.url)
            target = _cache_folder("loop", _digest(attachment.url)) / name
            await _fetch(target, lambda partial: _write_payload(partial, document.encode("utf-8")))
            return _saved(target, "text/plain; charset=utf-8")
        raise InvalidRequest("This attachment cannot be downloaded.")

    async def meeting_notes(self, url: str) -> str:
        """A Loop meeting-notes page as text, read now, under a line naming the page and the time it was
        read: the page is live, so later edits are not included."""
        notes = await self._notes.meeting_notes(url)
        read_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        head = (
            f"Meeting notes: {notes.title or 'untitled page'} (read from the live Loop page at {read_at}; "
            "later edits are not included)"
        )
        return f"{head}\n\n{notes.text}" if notes.text else head


class Photos:
    """Profile photos with a short in-process cache (a missing photo is cached too)."""

    def __init__(self, people: People) -> None:
        self._people = people
        self._cache: dict[str, tuple[float, tuple[bytes, str] | None]] = {}

    async def photo(self, user_id: str) -> tuple[bytes, str] | None:
        key = user_id.lower()
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        value = await self._people.photo(key)
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[key] = (time.monotonic() + PHOTO_TTL_SECONDS, value)
        return value


def _cache_folder(source: str, key: str) -> Path:
    folder = localfiles.kept_dir("downloads") / source / key
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _saved(target: Path, content_type: str | None) -> SavedFile:
    return SavedFile(
        path=str(target), name=target.name, content_type=content_type, size=target.stat().st_size
    )


async def _fetch(target: Path, fetch: Callable[[Path], Awaitable[RemoteFile]]) -> RemoteFile:
    """Fetch into a temporary file beside ``target`` and publish it under its name only when complete."""
    partial = target.with_name(f".{target.name}.{time.time_ns()}.part")
    try:
        remote = await fetch(partial)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return remote


async def _write_payload(partial: Path, payload: bytes) -> RemoteFile:
    partial.write_bytes(payload)
    return RemoteFile(name=None, content_type=None, size=len(payload))
