"""Export packaging (requirements §8, decided 2026-10-16), as the Outlook connector packages: exactly one
download.

- One conversation with no saved attachment is one flat file (.txt or .jsonl).
- Anything else is one ZIP: each conversation's flat file at its root, and its saved attachments in a
  folder named after that file. No ZIP is nested in another.

The final file is created exclusively in the exports directory (a numbered suffix on a name clash),
so concurrent exports never overwrite each other.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
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


@dataclass
class ConversationOutput:
    """One conversation's flat file and the attachments saved for it: (source file, name in its folder)."""

    text: Path
    attachments: list[tuple[Path, str]] = field(default_factory=list)


def package(outputs: list[ConversationOutput], *, zip_name: str) -> tuple[Path, str]:
    """Move the one flat file into the exports directory when there is nothing else, or write one ZIP: each
    flat file at its root, and its attachments in a folder named after it. The flat files' names are
    unique (the caller's stems), so their folders are too. Returns (path, content type)."""
    directory = localfiles.kept_dir("exports")
    if len(outputs) == 1 and not outputs[0].attachments:
        target = localfiles.claim(directory, outputs[0].text.name)
        shutil.move(str(outputs[0].text), str(target))
        return target, CONTENT_TYPES.get(target.suffix, "application/octet-stream")
    name = outputs[0].text.with_suffix(".zip").name if len(outputs) == 1 else zip_name
    target = localfiles.claim(directory, name)
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for output in outputs:
                archive.write(output.text, arcname=output.text.name)
                for path, asset in output.attachments:
                    archive.write(path, arcname=f"{output.text.stem}/{asset}")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target, CONTENT_TYPES[".zip"]
