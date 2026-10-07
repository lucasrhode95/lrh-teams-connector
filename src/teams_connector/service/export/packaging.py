"""Export packaging: one flat file when there is one output, otherwise one ZIP.

The final file is created exclusively in the exports directory (a numbered suffix on a name clash),
so concurrent exports never overwrite each other.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from teams_connector.domain.models import Chat
from teams_connector.service import localfiles

CONTENT_TYPES = {
    ".txt": "text/plain; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".zip": "application/zip",
}


def filename_stem(chat: Chat) -> str:
    label = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", chat.name)
    label = re.sub(r"\s+", " ", label).strip(" ._")[:70] or "chat"
    chat_id = re.sub(r"[^A-Za-z0-9]+", "-", chat.id).strip("-")[:32]
    return f"{label}-{chat_id}" if chat_id else label


def package(files: list[Path], *, zip_name: str) -> tuple[Path, str]:
    """Move one file, or zip several, into the exports directory. Returns (path, content type)."""
    directory = localfiles.kept_dir("exports")
    if len(files) == 1:
        target = localfiles.claim(directory, files[0].name)
        shutil.move(str(files[0]), str(target))
        return target, CONTENT_TYPES.get(target.suffix, "application/octet-stream")
    target = localfiles.claim(directory, zip_name)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=path.name)
    return target, CONTENT_TYPES[".zip"]
