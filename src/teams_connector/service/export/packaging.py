"""Export packaging: one flat file when there is one output, otherwise one ZIP.

The final file is created exclusively in the exports directory (a numbered suffix on a name clash),
so concurrent exports never overwrite each other.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from collections.abc import Iterable
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


def safe_asset_name(value: str | None) -> str:
    """Return one portable ZIP filename component for untrusted attachment metadata."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value or "file").strip(" .")[:150]
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "file"
    stem = Path(cleaned).stem.casefold()
    if stem in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }:
        cleaned = f"_{cleaned}"
    return cleaned


def unique_asset_name(value: str | None, used: set[str]) -> str:
    """Make sanitized attachment names unique without changing the first occurrence."""
    name = safe_asset_name(value)
    candidate = name
    stem, suffix = Path(name).stem, Path(name).suffix
    index = 2
    while candidate.casefold() in used:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def package_conversation(
    text_path: Path,
    assets: Iterable[tuple[Path, str]],
    *,
    folder_name: str,
    has_attachments: bool,
) -> Path:
    """Return a text file or a per-conversation ZIP with text and available attachments together."""
    asset_rows = list(assets)
    if not has_attachments:
        return text_path
    target = text_path.with_suffix(".zip")
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(text_path, arcname=f"{folder_name}/{text_path.name}")
            for path, name in asset_rows:
                archive.write(path, arcname=f"{folder_name}/{name}")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def package(files: list[Path], *, zip_name: str) -> tuple[Path, str]:
    """Move one file, or zip several, into the exports directory. Returns (path, content type)."""
    directory = localfiles.kept_dir("exports")
    if len(files) == 1:
        target = localfiles.claim(directory, files[0].name)
        shutil.move(str(files[0]), str(target))
        return target, CONTENT_TYPES.get(target.suffix, "application/octet-stream")
    target = localfiles.claim(directory, zip_name)
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target, CONTENT_TYPES[".zip"]
