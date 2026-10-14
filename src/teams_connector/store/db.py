"""Account-bound SQLite archive (architecture, "Requests, caching, and storage").

Holds what Teams cannot give back cheaply or at all: the chat catalog and its delta token, every
message the app has fetched (retained indefinitely, so content survives later edits and deletions),
chat rosters for participant search, and per-chat history progress. It is populated lazily by reads,
searches, exports and explicit syncs; there is no background collector.

Several short-lived processes may share it: WAL mode, busy timeout, one connection per operation,
short transactions. If SQLite reports it damaged when it is opened, the damaged files are moved to a
``corrupt-<timestamp>`` folder next to it and a fresh store is started.

Retention rules for a message seen again (requirements §6):
- an edit overwrites the text; no edit history is kept, and an unversioned or older record never
  replaces a known edit;
- a deletion keeps the last known text and marks the row deleted;
- a plain (stale) record never resurrects a deleted row; a later edit does.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sqlite3
import time
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from teams_connector import config
from teams_connector.domain.errors import AccountMismatch, ConnectorError, InvalidRequest
from teams_connector.domain.models import ArchiveTotals, Chat, MatchMode, Message, Participant

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
CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL,
    id TEXT NOT NULL,
    sent_at TEXT,
    edited_at TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    text_folded TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL,
    stored_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, id)
);
CREATE INDEX IF NOT EXISTS messages_time ON messages (conversation_id, sent_at, id);
CREATE INDEX IF NOT EXISTS messages_sent ON messages (sent_at);
CREATE TABLE IF NOT EXISTS participants (
    conversation_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    name_folded TEXT NOT NULL,
    PRIMARY KEY (conversation_id, participant_id, name)
);
CREATE INDEX IF NOT EXISTS participants_name ON participants (name_folded);
CREATE TABLE IF NOT EXISTS chat_state (
    conversation_id TEXT PRIMARY KEY,
    history_link TEXT,
    history_complete INTEGER NOT NULL DEFAULT 0,
    history_synced_at REAL,
    oldest_message_at TEXT,
    newest_message_at TEXT,
    missing_timestamps INTEGER NOT NULL DEFAULT 0,
    roster_complete INTEGER NOT NULL DEFAULT 0,
    roster_error TEXT,
    roster_synced_at REAL,
    unresolved_participants INTEGER NOT NULL DEFAULT 0,
    sync_attempted_at REAL
);
"""

log = logging.getLogger(__name__)

# Per-result fields: never stored with a message.
DERIVED_FIELDS = {"is_self", "text_truncated", "text_chars"}


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


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class ChatState:
    conversation_id: str
    history_link: str | None = None
    history_complete: bool = False
    history_synced_at: float | None = None
    oldest_message_at: str | None = None
    newest_message_at: str | None = None
    missing_timestamps: int = 0
    roster_complete: bool = False
    roster_error: str | None = None
    roster_synced_at: float | None = None
    unresolved_participants: int = 0
    sync_attempted_at: float | None = None


@dataclass
class ArchivedMessage:
    message: Message
    chat_name: str


class Store:
    def __init__(self, path: Path, owner: str) -> None:
        self.path = path
        self.owner = owner
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not self._healthy():
            self._quarantine()
        with contextlib.closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.execute("PRAGMA journal_mode=WAL")  # persistent: set once per database file
        with self._tx() as db:
            db.executescript(SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key = 'owner'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta (key, value) VALUES ('owner', ?)", (owner,))
            elif row[0] != owner:
                raise AccountMismatch(
                    f"The local archive at {path} belongs to a different Microsoft account."
                )

    def _healthy(self) -> bool:
        try:
            db = sqlite3.connect(self.path, timeout=15)
            try:
                return db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                db.close()
        except sqlite3.DatabaseError:
            return False

    def _quarantine(self) -> None:
        target = self.path.parent / f"corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        target.mkdir(exist_ok=True)
        try:
            for suffix in ("", "-wal", "-shm"):
                source = Path(f"{self.path}{suffix}")
                if source.exists():
                    os.replace(source, target / source.name)
        except PermissionError:
            raise ConnectorError(
                "The local archive is damaged and another teams-connector process is using it. "
                "Close the other connector processes (UI, MCP sessions) and retry."
            ) from None
        log.warning("Local archive was damaged; moved to %s and starting a fresh one.", target)

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

    def any_chat(self, chat_id: str) -> Chat | None:
        """A chat even when it left the catalog (its retained messages are still archived)."""
        with self._tx() as db:
            row = db.execute("SELECT data FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return Chat.model_validate_json(row[0]) if row else None

    # ---------------------------------------------------------------- messages

    def save_messages(self, conversation_id: str, messages: Iterable[Message]) -> int:
        """Merge fetched messages under the retention rules. Returns how many were new."""
        now = time.time()
        added = 0
        stamps: list[str] = []
        with self._tx() as db:
            for incoming in messages:
                row = db.execute(
                    "SELECT data FROM messages WHERE conversation_id = ? AND id = ?",
                    (conversation_id, incoming.id),
                ).fetchone()
                existing = Message.model_validate_json(row[0]) if row else None
                merged = _merge_message(existing, incoming)
                if existing is None:
                    added += 1
                if merged.sent_at:
                    stamps.append(iso(merged.sent_at) or "")
                db.execute(
                    """INSERT OR REPLACE INTO messages (conversation_id, id, sent_at, edited_at, is_deleted,
                           deleted_at, text_folded, data, stored_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        conversation_id,
                        merged.id,
                        iso(merged.sent_at),
                        iso(merged.edited_at),
                        int(merged.is_deleted),
                        iso(merged.deleted_at),
                        fold(merged.text),
                        merged.model_dump_json(exclude=DERIVED_FIELDS),
                        now,
                    ),
                )
            if stamps:
                db.execute(
                    """INSERT INTO chat_state (conversation_id, oldest_message_at, newest_message_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT (conversation_id) DO UPDATE SET
                           oldest_message_at = MIN(COALESCE(oldest_message_at, excluded.oldest_message_at),
                                                   excluded.oldest_message_at),
                           newest_message_at = MAX(COALESCE(newest_message_at, excluded.newest_message_at),
                                                   excluded.newest_message_at)""",
                    (conversation_id, min(stamps), max(stamps)),
                )
        return added

    def message(self, conversation_id: str, message_id: str) -> Message | None:
        with self._tx() as db:
            row = db.execute(
                "SELECT data FROM messages WHERE conversation_id = ? AND id = ?",
                (conversation_id, message_id),
            ).fetchone()
        return Message.model_validate_json(row[0]) if row else None

    def newest_first(
        self,
        conversation_id: str,
        *,
        since: datetime | None,
        until: datetime | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[Message]:
        """Archived messages newest first, strictly before the (sent_at, id) keyset position."""
        where, args = self._range(conversation_id, since, until)
        if before is not None:
            where += " AND (sent_at < ? OR (sent_at = ? AND id < ?))"
            args += [before[0], before[0], before[1]]
        with self._tx() as db:
            rows = db.execute(
                f"SELECT data FROM messages WHERE {where} ORDER BY sent_at DESC, id DESC LIMIT ?",
                [*args, limit],
            ).fetchall()
        return [Message.model_validate_json(r[0]) for r in rows]

    def chronological(
        self,
        conversation_id: str,
        *,
        since: datetime | None,
        until: datetime | None,
        after: tuple[str, str] | None = None,
        from_key: tuple[str, str] | None = None,
        limit: int = 500,
    ) -> list[Message]:
        where, args = self._range(conversation_id, since, until)
        if after is not None:
            where += " AND (sent_at, id) > (?, ?)"
            args += list(after)
        if from_key is not None:
            where += " AND (sent_at, id) >= (?, ?)"
            args += list(from_key)
        with self._tx() as db:
            rows = db.execute(
                f"SELECT data FROM messages WHERE {where} ORDER BY sent_at ASC, id ASC LIMIT ?",
                [*args, limit],
            ).fetchall()
        return [Message.model_validate_json(r[0]) for r in rows]

    def count(self, conversation_id: str, *, since: datetime | None, until: datetime | None) -> int:
        where, args = self._range(conversation_id, since, until)
        with self._tx() as db:
            return int(db.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", args).fetchone()[0])

    def latest_cutoff(
        self, conversation_id: str, *, since: datetime | None, until: datetime | None, limit: int
    ) -> tuple[str, str] | None:
        """The (sent_at, id) of the ``limit``-th newest message in range: the oldest of the latest N."""
        if limit < 1:
            return None
        where, args = self._range(conversation_id, since, until)
        with self._tx() as db:
            row = db.execute(
                f"SELECT sent_at, id FROM messages WHERE {where} "
                "ORDER BY sent_at DESC, id DESC LIMIT 1 OFFSET ?",
                [*args, limit - 1],
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    @staticmethod
    def _range(conversation_id: str, since: datetime | None, until: datetime | None) -> tuple[str, list]:
        where = "conversation_id = ? AND sent_at IS NOT NULL"
        args: list = [conversation_id]
        if since is not None:
            where += " AND sent_at >= ?"
            args.append(iso(since))
        if until is not None:
            where += " AND sent_at <= ?"
            args.append(iso(until))
        return where, args

    def search(
        self,
        query: str,
        *,
        mode: MatchMode,
        conversation_ids: list[str],
        since: datetime,
        until: datetime,
        after: tuple[str, str, str] | None,
        limit: int,
    ) -> tuple[list[ArchivedMessage], bool]:
        """Literal matching over folded text, newest first, one stable keyset page."""
        normalized = fold(query)
        predicates = ["m.sent_at IS NOT NULL", "m.sent_at >= ?", "m.sent_at <= ?"]
        args: list = [iso(since), iso(until)]
        if mode == "exact":
            predicates.append("m.text_folded = ?")
            args.append(normalized)
        elif mode == "substring":
            predicates.append("m.text_folded LIKE ? ESCAPE '\\'")
            args.append(f"%{_like_literal(normalized)}%")
        else:
            terms = re.findall(r"\w+", normalized, flags=re.UNICODE)
            if not terms:
                raise InvalidRequest("query must contain at least one searchable word.")
            for term in terms:
                predicates.append("m.text_folded LIKE ? ESCAPE '\\'")
                args.append(f"%{_like_literal(term)}%")
        if after is not None:
            predicates.append("(m.sent_at, m.conversation_id, m.id) < (?, ?, ?)")
            args += list(after)
        results: list[tuple[str, str, str, str]] = []
        with self._tx() as db:
            db.execute("CREATE TEMP TABLE IF NOT EXISTS scope (id TEXT PRIMARY KEY)")
            db.execute("DELETE FROM scope")
            db.executemany("INSERT OR IGNORE INTO scope (id) VALUES (?)", [(i,) for i in conversation_ids])
            rows = db.execute(
                f"""SELECT m.data, c.data, m.sent_at, m.conversation_id, m.id
                    FROM messages m JOIN scope s ON s.id = m.conversation_id
                    LEFT JOIN chats c ON c.id = m.conversation_id
                    WHERE {" AND ".join(predicates)}
                    ORDER BY m.sent_at DESC, m.conversation_id DESC, m.id DESC LIMIT ?""",
                [*args, limit + 1],
            ).fetchall()
            results = [(r[0], r[1], r[2], r[3]) for r in rows]
        hits = [
            ArchivedMessage(
                Message.model_validate_json(data),
                Chat.model_validate_json(chat).name if chat else conversation_id,
            )
            for data, chat, _, conversation_id in results[:limit]
        ]
        return hits, len(results) > limit

    # ---------------------------------------------------------------- participants

    def save_roster(
        self, conversation_id: str, participants: list[Participant] | None, *, error: str | None = None
    ) -> None:
        """Replace a chat's roster (or record why it could not be read; the previous one is kept)."""
        now = time.time()
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO chat_state (conversation_id) VALUES (?)", (conversation_id,))
            if error is not None or participants is None:
                db.execute(
                    "UPDATE chat_state SET roster_error = ?, roster_synced_at = ? WHERE conversation_id = ?",
                    (error, now, conversation_id),
                )
                return
            db.execute("DELETE FROM participants WHERE conversation_id = ?", (conversation_id,))
            db.executemany(
                """INSERT OR IGNORE INTO participants (conversation_id, participant_id, name, name_folded)
                   VALUES (?, ?, ?, ?)""",
                [(conversation_id, p.id or "", p.name, fold(p.name)) for p in participants if p.name],
            )
            db.execute(
                """UPDATE chat_state SET roster_complete = 1, roster_error = NULL, roster_synced_at = ?,
                       unresolved_participants = ? WHERE conversation_id = ?""",
                (now, sum(1 for p in participants if not p.name), conversation_id),
            )

    def participant_matches(self, query: str) -> dict[str, list[Participant]]:
        """Participants whose name contains the query, by conversation."""
        out: dict[str, list[Participant]] = {}
        with self._tx() as db:
            rows = db.execute(
                """SELECT p.conversation_id, p.participant_id, p.name FROM participants p
                   JOIN chats c ON c.id = p.conversation_id AND c.is_deleted = 0
                   WHERE instr(p.name_folded, ?) > 0 ORDER BY p.name""",
                (fold(query),),
            ).fetchall()
        for conversation_id, participant_id, name in rows:
            out.setdefault(conversation_id, []).append(Participant(id=participant_id or None, name=name))
        return out

    # ---------------------------------------------------------------- per-chat state

    def state(self, conversation_id: str) -> ChatState:
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM chat_state WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
        return _state(row) if row else ChatState(conversation_id)

    def states(self) -> dict[str, ChatState]:
        with self._tx() as db:
            rows = db.execute("SELECT * FROM chat_state").fetchall()
        return {row["conversation_id"]: _state(row) for row in rows}

    def save_history_progress(
        self, conversation_id: str, *, link: str | None, complete: bool, missing_timestamps: int = 0
    ) -> None:
        with self._tx() as db:
            db.execute(
                """INSERT INTO chat_state (conversation_id, history_link, history_complete, history_synced_at,
                       missing_timestamps)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (conversation_id) DO UPDATE SET history_link = excluded.history_link,
                       history_complete = excluded.history_complete,
                       history_synced_at = excluded.history_synced_at,
                       missing_timestamps = chat_state.missing_timestamps + excluded.missing_timestamps""",
                (conversation_id, link, int(complete), time.time(), missing_timestamps),
            )

    def touch_sync_attempt(self, conversation_id: str) -> None:
        with self._tx() as db:
            db.execute(
                """INSERT INTO chat_state (conversation_id, sync_attempted_at) VALUES (?, ?)
                   ON CONFLICT (conversation_id) DO UPDATE SET
                       sync_attempted_at = excluded.sync_attempted_at""",
                (conversation_id, time.time()),
            )

    def stored_counts(self) -> dict[str, int]:
        with self._tx() as db:
            rows = db.execute(
                "SELECT conversation_id, COUNT(*) FROM messages GROUP BY conversation_id"
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def totals(self) -> ArchiveTotals:
        with self._tx() as db:
            one = lambda sql: int(db.execute(sql).fetchone()[0])  # noqa: E731
            return ArchiveTotals(
                chats=one("SELECT COUNT(*) FROM chats WHERE is_deleted = 0"),
                messages=one("SELECT COUNT(*) FROM messages"),
                participants=one("SELECT COUNT(*) FROM participants"),
                complete_histories=one("SELECT COUNT(*) FROM chat_state WHERE history_complete = 1"),
                complete_rosters=one("SELECT COUNT(*) FROM chat_state WHERE roster_complete = 1"),
            )

    # ---------------------------------------------------------------- backup

    def backup_to(self, destination: str | Path, *, overwrite: bool = False) -> Path:
        """A consistent online copy (SQLite backup API), owner metadata included."""
        target = Path(destination).expanduser().resolve()
        if target == self.path.resolve():
            raise InvalidRequest("The backup destination is the live archive itself.")
        if target.exists() and not overwrite:
            raise InvalidRequest(f"{target} already exists; pass --overwrite to replace it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.unlink(missing_ok=True)
        try:
            with (
                contextlib.closing(sqlite3.connect(self.path, timeout=15)) as source,
                contextlib.closing(sqlite3.connect(temporary)) as copy,
            ):
                source.backup(copy)
            shutil.move(str(temporary), str(target))
        finally:
            temporary.unlink(missing_ok=True)
        return target


def _state(row: sqlite3.Row) -> ChatState:
    return ChatState(
        conversation_id=row["conversation_id"],
        history_link=row["history_link"],
        history_complete=bool(row["history_complete"]),
        history_synced_at=row["history_synced_at"],
        oldest_message_at=row["oldest_message_at"],
        newest_message_at=row["newest_message_at"],
        missing_timestamps=int(row["missing_timestamps"] or 0),
        roster_complete=bool(row["roster_complete"]),
        roster_error=row["roster_error"],
        roster_synced_at=row["roster_synced_at"],
        unresolved_participants=int(row["unresolved_participants"] or 0),
        sync_attempted_at=row["sync_attempted_at"],
    )


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


def _merge_message(existing: Message | None, incoming: Message) -> Message:
    if existing is None:
        return incoming
    previous_edit = iso(existing.edited_at) or ""
    previous_delete = iso(existing.deleted_at) or ""
    incoming_edit = iso(incoming.edited_at) or ""
    if incoming.is_deleted:
        # A deletion: keep the last known text (and the original metadata), mark the row deleted.
        keep = existing.model_copy()
        if not keep.text and incoming.text:
            keep.text = incoming.text
        keep.is_deleted = True
        keep.deleted_at = max((d for d in (existing.deleted_at, incoming.deleted_at) if d), default=None)
        keep.edited_at = existing.edited_at or incoming.edited_at
        return keep
    if existing.is_deleted and not (incoming_edit and incoming_edit > max(previous_delete, previous_edit)):
        return existing  # a stale plain record is no evidence that the message was restored
    if previous_edit and (not incoming_edit or incoming_edit < previous_edit):
        return existing  # never let an older or unversioned record replace known edited text
    merged = incoming.model_copy()
    for key in ("author", "author_id", "sent_at", "subject", "reply_to_id", "system_event"):
        if getattr(merged, key) is None and getattr(existing, key) is not None:
            setattr(merged, key, getattr(existing, key))
    if not merged.attachments and existing.attachments:
        merged.attachments = list(existing.attachments)
    return merged
