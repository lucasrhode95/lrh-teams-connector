"""Account-bound SQLite store for the chat catalog only (architecture, "Requests, caching, and storage").

It holds the chat catalog and its delta token, and the account's metadata. It holds no message content:
Teams is the record, and every history read goes to Teams. Several short-lived processes may share the file:
WAL mode, busy timeout, one connection per operation, short transactions. If SQLite reports it damaged when
it is opened, the damaged files are moved to a ``corrupt-<timestamp>`` folder next to it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from teams_connector import config
from teams_connector.domain.errors import AccountMismatch, ConnectorError
from teams_connector.domain.models import Chat

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    last_message_at TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    catalogued_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chats_activity ON chats (last_message_at);
"""

log = logging.getLogger(__name__)


def store_path(fingerprint: str) -> Path:
    return config.data_dir() / "accounts" / fingerprint / "teams.sqlite3"


def iso(value: datetime | None) -> str | None:
    """Fixed-width UTC text, so SQL comparisons order correctly."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def fold(text: str) -> str:
    """Search normalization: NFKC, case folding, collapsed whitespace. Accents stay significant."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


class Store:
    def __init__(self, path: Path, owner: str) -> None:
        self.path = path
        self.owner = owner
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not self._healthy():
            self._move_aside("corrupt", "Local store was damaged; moved to %s and starting a fresh one.")
        with contextlib.closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.execute("PRAGMA journal_mode=WAL")  # persistent: set once per database file
        with self._tx() as db:
            db.executescript(SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key = 'owner'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta (key, value) VALUES ('owner', ?)", (owner,))
            elif row[0] != owner:
                raise AccountMismatch(f"The local store at {path} belongs to a different Microsoft account.")

    def _healthy(self) -> bool:
        try:
            db = sqlite3.connect(self.path, timeout=15)
            try:
                return db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                db.close()
        except sqlite3.DatabaseError:
            return False

    def _move_aside(self, folder_prefix: str, message: str) -> None:
        """Move the store's files into a timestamped folder next to it. Nothing is deleted."""
        target = self.path.parent / f"{folder_prefix}-{time.strftime('%Y%m%d-%H%M%S')}"
        target.mkdir(exist_ok=True)
        try:
            for suffix in ("", "-wal", "-shm"):
                source = Path(f"{self.path}{suffix}")
                if source.exists():
                    os.replace(source, target / source.name)
        except PermissionError:
            raise ConnectorError(
                "The local store is in use by another teams-connector process. "
                "Close the other connector processes (UI, MCP sessions) and retry."
            ) from None
        log.warning(message, target)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)  # timeout = busy timeout for concurrent processes
        db.row_factory = sqlite3.Row
        try:
            with db:  # one transaction
                yield db
        finally:
            db.close()

    # ---------------------------------------------------------------- meta

    def meta(self, key: str) -> str | None:
        with self._tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_meta(self, **values: str | None) -> None:
        with self._tx() as db:
            for key, value in values.items():
                if value is None:
                    db.execute("DELETE FROM meta WHERE key = ?", (key,))
                else:
                    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ---------------------------------------------------------------- chats

    def save_catalog(
        self,
        chats: Iterable[Chat],
        *,
        deleted_ids: Iterable[str] = (),
        meta: dict[str, str] | None = None,
    ) -> None:
        """Merge chats (and deletions) and their continuation token in one transaction."""
        now = time.time()
        with self._tx() as db:
            for chat in chats:
                row = db.execute("SELECT data FROM chats WHERE id = ?", (chat.id,)).fetchone()
                merged = _merge_chat(Chat.model_validate_json(row[0]) if row else None, chat)
                db.execute(
                    """INSERT INTO chats (id, data, last_message_at, is_deleted, catalogued_at)
                       VALUES (?, ?, ?, 0, ?)
                       ON CONFLICT (id) DO UPDATE SET data = excluded.data,
                           last_message_at = excluded.last_message_at, is_deleted = 0,
                           catalogued_at = excluded.catalogued_at""",
                    (merged.id, merged.model_dump_json(), iso(merged.last_message_at), now),
                )
            db.executemany(
                "UPDATE chats SET is_deleted = 1, catalogued_at = ? WHERE id = ?",
                [(now, chat_id) for chat_id in deleted_ids],
            )
            for key, value in (meta or {}).items():
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def chats(self, *, kind: str | None = None) -> list[Chat]:
        """Live catalog, most recent activity first (chats without activity last)."""
        sql = "SELECT data FROM chats WHERE is_deleted = 0"
        with self._tx() as db:
            rows = db.execute(
                sql + " ORDER BY last_message_at IS NULL, last_message_at DESC, rowid"
            ).fetchall()
        chats = [Chat.model_validate_json(r[0]) for r in rows]
        return [c for c in chats if c.kind == kind] if kind else chats

    def chat(self, chat_id: str) -> Chat | None:
        with self._tx() as db:
            row = db.execute("SELECT data FROM chats WHERE id = ? AND is_deleted = 0", (chat_id,)).fetchone()
        return Chat.model_validate_json(row[0]) if row else None


def _merge_chat(existing: Chat | None, incoming: Chat) -> Chat:
    """New values win; a missing value keeps the known one, and an unresolved name (the id) never
    replaces a resolved one."""
    if existing is None:
        return incoming
    data = incoming.model_dump()
    for key, value in existing.model_dump().items():
        if data.get(key) is None and value is not None:
            data[key] = value
    if incoming.name == incoming.id and existing.name != existing.id:
        data["name"] = existing.name
    return Chat.model_validate(data)
