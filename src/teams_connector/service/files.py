"""Attachment downloads (MCP and UI) and profile photos.

Only attachments of archived messages can be downloaded: no arbitrary URL is ever fetched. Files are
saved under ``<data directory>/downloads`` and removed after a week.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from teams_connector.domain.errors import InvalidRequest, NotFound
from teams_connector.domain.models import Attachment, SavedFile
from teams_connector.remote.ports import FileReader, People, RemoteFile
from teams_connector.service import localfiles
from teams_connector.store.db import Store

PHOTO_TTL_SECONDS = 3600
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_filename(name: str, fallback: str = "file") -> str:
    cleaned = _UNSAFE.sub("_", name).strip(" .")
    return cleaned[:150] or fallback


class Files:
    def __init__(self, store: Store, reader: FileReader) -> None:
        self._store = store
        self._reader = reader

    async def download_attachment(
        self, conversation_id: str, message_id: str, name: str | None = None
    ) -> SavedFile:
        message = self._store.message(conversation_id, message_id)
        if message is None:
            raise NotFound(
                "The message is not in the local archive; read its conversation with get_messages first."
            )
        files = [a for a in message.attachments if a.url]
        if name:
            files = [a for a in files if a.name == name]
        if not files:
            raise NotFound(
                "The message has no downloadable attachment" + (f" named {name!r}." if name else ".")
            )
        if len(files) > 1:
            names = ", ".join(repr(a.name) for a in files)
            raise InvalidRequest(f"The message has several attachments; pass name (one of {names}).")
        attachment = files[0]
        directory = localfiles.kept_dir("downloads")
        partial = directory / f".download-{time.time_ns()}.part"
        try:
            remote = await self._reader.download(attachment.url or "", partial)
            target = localfiles.claim(directory, safe_filename(remote.name or attachment.name))
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return SavedFile(
            path=str(target), name=target.name, content_type=remote.content_type, size=remote.size
        )

    async def download_selected_attachment(
        self, attachment: Attachment, destination: Path, *, max_bytes: int
    ) -> RemoteFile:
        """Fetch a selected message attachment into caller-owned temporary storage."""
        if not attachment.url or not attachment.url.lower().startswith("https://"):
            raise InvalidRequest("Only HTTPS message attachments can be included in an export.")
        return await self._reader.download(attachment.url, destination, max_bytes=max_bytes)


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
