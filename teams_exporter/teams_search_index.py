from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from teams_exporter.teams_connector import ChatSummary, TeamsConnector
from teams_exporter.teams_message_formatter import TeamsMessageFormatter


class DatabaseOwnerMismatchError(RuntimeError):
    """Raised when an index path is already bound to another Teams account."""


class TeamsSearchIndex:
    """Local SQLite catalog, message archive, and FTS5 search index."""

    OWNER_METADATA_KEY = "account_fingerprint"

    def __init__(
        self,
        path: str | Path = ".teams_search_index.sqlite3",
        *,
        owner_fingerprint: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.owner_fingerprint = owner_fingerprint
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def resolve_account_path(
        cls,
        base_path: str | Path,
        owner_fingerprint: str,
        *,
        use_base_if_missing: bool = False,
    ) -> tuple[Path, str | None]:
        """Choose an index file without adopting or overwriting another user's data."""
        base = Path(base_path)
        warning = None
        if base.exists():
            base_owner = cls.read_owner_fingerprint(base)
            if base_owner == owner_fingerprint:
                return base, None
            if base_owner is None:
                warning = (
                    "An existing Teams archive has no verified account owner. "
                    "It was preserved, and a separate archive was opened for this account."
                )
            else:
                warning = (
                    "An existing Teams archive belongs to another account. "
                    "It was preserved, and a separate archive was opened for this account."
                )
        elif use_base_if_missing:
            return base, None

        suffix = base.suffix or ".sqlite3"
        stem = base.stem if base.suffix else base.name
        short_path = base.with_name(f"{stem}-{owner_fingerprint[:16]}{suffix}")
        if not short_path.exists():
            return short_path, warning
        if cls.read_owner_fingerprint(short_path) == owner_fingerprint:
            return short_path, warning

        full_path = base.with_name(f"{stem}-{owner_fingerprint}{suffix}")
        if not full_path.exists():
            return full_path, warning
        if cls.read_owner_fingerprint(full_path) == owner_fingerprint:
            return full_path, warning
        raise DatabaseOwnerMismatchError(
            "An account-specific archive path is occupied by a database with a different or unknown owner. "
            "The existing file was left untouched. Set TEAMS_SEARCH_INDEX_PATH to another location."
        )

    @classmethod
    def read_owner_fingerprint(cls, path: str | Path) -> str | None:
        """Read ownership metadata without creating or changing the database."""
        database_path = Path(path)
        if not database_path.is_file():
            return None
        try:
            uri = database_path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
                has_metadata = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'index_metadata'"
                ).fetchone()
                if not has_metadata:
                    return None
                row = connection.execute(
                    "SELECT value FROM index_metadata WHERE key = ?",
                    (cls.OWNER_METADATA_KEY,),
                ).fetchone()
                return str(row[0]) if row else None
        except (OSError, sqlite3.Error, ValueError):
            return None

    def backup_to(self, destination: str | Path, *, overwrite: bool = False) -> Path:
        """Create a transactionally consistent SQLite backup at ``destination``."""
        target = Path(destination).expanduser().resolve()
        source = self.path.resolve()
        if target == source:
            raise ValueError("The backup destination must differ from the active archive.")
        if target.exists() and not overwrite:
            raise FileExistsError(f"Backup destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".sqlite3", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            source_connection = sqlite3.connect(source, timeout=30)
            try:
                destination_connection = sqlite3.connect(temporary, timeout=30)
                try:
                    source_connection.backup(destination_connection)
                    destination_connection.commit()
                finally:
                    destination_connection.close()
            finally:
                source_connection.close()
            if overwrite:
                os.replace(temporary, target)
            else:
                # Hard-link creation fails atomically if another process made the
                # destination after our initial existence check.
                os.link(temporary, target)
                temporary.unlink()
            return target
        finally:
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                existing_owner = self._owner_from_connection(connection)
                if (
                    self.owner_fingerprint is not None
                    and existing_owner is not None
                    and existing_owner != self.owner_fingerprint
                ):
                    raise DatabaseOwnerMismatchError(
                        "The selected Teams archive belongs to another account."
                    )
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS chats (
                        conversation_id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        chat_type TEXT,
                        last_message_at TEXT,
                        last_message_preview TEXT,
                        unread INTEGER,
                        catalogued_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        row_id INTEGER PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        author TEXT NOT NULL,
                        author_id TEXT,
                        sent_at TEXT,
                        body TEXT NOT NULL,
                        message_type TEXT,
                        indexed_at TEXT NOT NULL,
                        UNIQUE (conversation_id, message_id),
                        FOREIGN KEY (conversation_id) REFERENCES chats(conversation_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS messages_conversation_time
                        ON messages(conversation_id, sent_at);
                    CREATE TABLE IF NOT EXISTS participants (
                        conversation_id TEXT NOT NULL,
                        participant_id TEXT,
                        display_name TEXT NOT NULL,
                        display_name_folded TEXT NOT NULL,
                        PRIMARY KEY (conversation_id, participant_id, display_name),
                        FOREIGN KEY (conversation_id) REFERENCES chats(conversation_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS participants_by_name
                        ON participants(display_name_folded);
                    CREATE TABLE IF NOT EXISTS chat_index_state (
                        conversation_id TEXT PRIMARY KEY,
                        messages_next_url TEXT,
                        message_history_complete INTEGER NOT NULL DEFAULT 0,
                        oldest_message_at TEXT,
                        newest_message_at TEXT,
                        messages_indexed_at TEXT,
                        messages_stored INTEGER NOT NULL DEFAULT 0,
                        messages_scanned INTEGER NOT NULL DEFAULT 0,
                        message_pages INTEGER NOT NULL DEFAULT 0,
                        missing_timestamp_messages INTEGER NOT NULL DEFAULT 0,
                        participant_sync_complete INTEGER NOT NULL DEFAULT 0,
                        participant_sync_error TEXT,
                        participants_indexed_at TEXT,
                        participants_scanned INTEGER NOT NULL DEFAULT 0,
                        unresolved_participants INTEGER NOT NULL DEFAULT 0,
                        sync_attempted_at TEXT,
                        FOREIGN KEY (conversation_id) REFERENCES chats(conversation_id)
                            ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS index_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT OR IGNORE INTO index_metadata(key, value)
                        VALUES ('generation', '0');
                    CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
                        conversation_id UNINDEXED,
                        message_id UNINDEXED,
                        body,
                        tokenize = 'unicode61'
                    );
                    """
                )
                message_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(messages)")
                }
                if "author_id" not in message_columns:
                    connection.execute("ALTER TABLE messages ADD COLUMN author_id TEXT")
                if self.owner_fingerprint is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO index_metadata(key, value) VALUES (?, ?)",
                        (self.OWNER_METADATA_KEY, self.owner_fingerprint),
                    )
                    stored_owner = self._owner_from_connection(connection)
                    if stored_owner != self.owner_fingerprint:
                        raise DatabaseOwnerMismatchError(
                            "The selected Teams archive belongs to another account."
                        )
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).casefold() or "no such module" in str(exc).casefold():
                raise RuntimeError("This Python SQLite build does not include FTS5.") from exc
            raise

    def _owner_from_connection(self, connection: sqlite3.Connection) -> str | None:
        has_metadata = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'index_metadata'"
        ).fetchone()
        if not has_metadata:
            return None
        row = connection.execute(
            "SELECT value FROM index_metadata WHERE key = ?",
            (self.OWNER_METADATA_KEY,),
        ).fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def upsert_chats(self, chats: list[ChatSummary]) -> None:
        now = self._now()
        with self._connect() as connection:
            for chat in chats:
                connection.execute(
                    """INSERT INTO chats (
                        conversation_id, display_name, chat_type, last_message_at,
                        last_message_preview, unread, catalogued_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        chat_type = excluded.chat_type,
                        last_message_at = excluded.last_message_at,
                        last_message_preview = excluded.last_message_preview,
                        unread = excluded.unread,
                        catalogued_at = excluded.catalogued_at""",
                    (
                        chat.id,
                        chat.display_name,
                        chat.chat_type,
                        self._timestamp(chat.last_message_at),
                        chat.last_message_preview,
                        None if chat.unread is None else int(chat.unread),
                        now,
                    ),
                )

    def get_state(self, conversation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chat_index_state WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else {
            "conversation_id": conversation_id,
            "messages_next_url": None,
            "message_history_complete": 0,
            "oldest_message_at": None,
            "newest_message_at": None,
            "messages_indexed_at": None,
            "messages_stored": 0,
            "messages_scanned": 0,
            "message_pages": 0,
            "missing_timestamp_messages": 0,
            "participant_sync_complete": 0,
            "participant_sync_error": None,
            "participants_indexed_at": None,
            "participants_scanned": 0,
            "unresolved_participants": 0,
            "sync_attempted_at": None,
        }

    def sync_attempt_times(self) -> dict[str, str | None]:
        """Return persisted per-chat attempt times for fair bounded sync batches."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT conversation_id, sync_attempted_at FROM chat_index_state"
            ).fetchall()
        return {row["conversation_id"]: row["sync_attempted_at"] for row in rows}

    def touch_sync_attempt(self, conversation_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO chat_index_state(conversation_id, sync_attempted_at)
                   VALUES (?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                     sync_attempted_at = excluded.sync_attempted_at""",
                (conversation_id, self._now()),
            )

    def reset_message_progress(self, conversation_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO chat_index_state(conversation_id)
                   VALUES (?) ON CONFLICT(conversation_id) DO UPDATE SET
                     messages_next_url = NULL,
                     message_history_complete = 0""",
                (conversation_id,),
            )

    def save_message_page(
        self,
        conversation_id: str,
        messages: list[dict[str, Any]],
        *,
        next_url: str | None,
        raw_scanned: int,
        missing_timestamp_count: int,
        formatter: TeamsMessageFormatter,
        history_complete: bool | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        with self._connect() as connection:
            prior = connection.execute(
                "SELECT * FROM chat_index_state WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if history_complete is None:
                history_complete = next_url is None
            oldest = prior["oldest_message_at"] if prior else None
            newest = prior["newest_message_at"] if prior else None
            stored = 0
            for message in messages:
                body = formatter.format_message(message)
                author = self._message_author(message, body)
                author_id = TeamsConnector._message_author_id(message)
                timestamp = self._timestamp(TeamsConnector._message_datetime(message))
                message_id = message.get("id")
                if message_id is None:
                    fingerprint = json.dumps(
                        [conversation_id, message.get("sequenceId"), timestamp, author, body],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    message_id = "generated:" + hashlib.sha256(
                        fingerprint.encode("utf-8")
                    ).hexdigest()
                else:
                    message_id = str(message_id)

                message_type = message.get("messagetype") or message.get("messageType")
                if not isinstance(message_type, str):
                    message_type = None
                existing = connection.execute(
                    "SELECT row_id FROM messages WHERE conversation_id = ? AND message_id = ?",
                    (conversation_id, message_id),
                ).fetchone()
                if existing:
                    connection.execute(
                        "DELETE FROM message_fts WHERE rowid = ?", (existing["row_id"],)
                    )
                    connection.execute(
                        """UPDATE messages SET author = ?, author_id = COALESCE(?, author_id),
                           sent_at = ?, body = ?, message_type = ?, indexed_at = ?
                           WHERE row_id = ?""",
                        (
                            author,
                            author_id,
                            timestamp,
                            body,
                            message_type,
                            now,
                            existing["row_id"],
                        ),
                    )
                    row_id = existing["row_id"]
                else:
                    cursor = connection.execute(
                        """INSERT INTO messages (
                            conversation_id, message_id, author, author_id, sent_at,
                            body, message_type, indexed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            conversation_id,
                            message_id,
                            author,
                            author_id,
                            timestamp,
                            body,
                            message_type,
                            now,
                        ),
                    )
                    row_id = cursor.lastrowid
                    stored += 1
                connection.execute(
                    "INSERT INTO message_fts(rowid, conversation_id, message_id, body) "
                    "VALUES (?, ?, ?, ?)",
                    (row_id, conversation_id, message_id, body),
                )
                if timestamp:
                    oldest = timestamp if oldest is None else min(oldest, timestamp)
                    newest = timestamp if newest is None else max(newest, timestamp)

            connection.execute(
                """INSERT INTO chat_index_state (
                    conversation_id, messages_next_url, message_history_complete,
                    oldest_message_at, newest_message_at, messages_indexed_at,
                    messages_stored, messages_scanned, message_pages,
                    missing_timestamp_messages
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    messages_next_url = excluded.messages_next_url,
                    message_history_complete = excluded.message_history_complete,
                    oldest_message_at = excluded.oldest_message_at,
                    newest_message_at = excluded.newest_message_at,
                    messages_indexed_at = excluded.messages_indexed_at,
                    messages_stored = chat_index_state.messages_stored + excluded.messages_stored,
                    messages_scanned = chat_index_state.messages_scanned + excluded.messages_scanned,
                    message_pages = chat_index_state.message_pages + 1,
                    missing_timestamp_messages = chat_index_state.missing_timestamp_messages
                        + excluded.missing_timestamp_messages""",
                (
                    conversation_id,
                    next_url,
                    int(history_complete),
                    oldest,
                    newest,
                    now,
                    stored,
                    raw_scanned,
                    missing_timestamp_count,
                ),
            )
            self._bump_generation(connection)
            updated = connection.execute(
                "SELECT * FROM chat_index_state WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        result = dict(updated)
        result["messages_added_this_page"] = stored
        return result

    def save_participants(
        self,
        conversation_id: str,
        participants: list[dict[str, str | None]],
        *,
        error: str | None = None,
        scanned_count: int | None = None,
        unresolved_count: int = 0,
    ) -> None:
        if scanned_count is None:
            scanned_count = len(participants) + unresolved_count
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO chat_index_state(conversation_id)
                   VALUES (?) ON CONFLICT(conversation_id) DO NOTHING""",
                (conversation_id,),
            )
            if error is None:
                connection.execute(
                    "DELETE FROM participants WHERE conversation_id = ?", (conversation_id,)
                )
                for participant in participants:
                    name = participant.get("display_name")
                    if not name:
                        continue
                    connection.execute(
                        """INSERT OR IGNORE INTO participants(
                            conversation_id, participant_id, display_name, display_name_folded
                        ) VALUES (?, ?, ?, ?)""",
                        (
                            conversation_id,
                            participant.get("id") or "",
                            name,
                            name.casefold(),
                        ),
                    )
            connection.execute(
                """UPDATE chat_index_state SET participant_sync_complete = ?,
                    participant_sync_error = ?, participants_indexed_at = ?,
                    participants_scanned = CASE WHEN ? IS NULL THEN ? ELSE participants_scanned END,
                    unresolved_participants = CASE WHEN ? IS NULL THEN ? ELSE unresolved_participants END
                   WHERE conversation_id = ?""",
                (
                    int(error is None),
                    error,
                    now,
                    error,
                    scanned_count,
                    error,
                    unresolved_count,
                    conversation_id,
                ),
            )
            self._bump_generation(connection)

    def search_messages(
        self,
        query: str,
        *,
        match_mode: str,
        conversation_id: str | None,
        since: str | None,
        until: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        match = self._fts_query(query, match_mode)
        predicates = ["message_fts MATCH ?"]
        parameters: list[Any] = [match]
        if conversation_id:
            predicates.append("m.conversation_id = ?")
            parameters.append(conversation_id)
        if since:
            predicates.append("m.sent_at IS NOT NULL AND m.sent_at >= ?")
            parameters.append(since)
        if until:
            predicates.append("m.sent_at IS NOT NULL AND m.sent_at <= ?")
            parameters.append(until)
        where = " AND ".join(predicates)
        with self._connect() as connection:
            total = connection.execute(
                f"""SELECT COUNT(*) FROM message_fts
                    JOIN messages m ON m.row_id = message_fts.rowid
                    WHERE {where}""",
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""SELECT m.conversation_id, m.message_id, m.author, m.author_id,
                           m.sent_at, m.body, m.message_type, c.display_name
                    FROM message_fts JOIN messages m ON m.row_id = message_fts.rowid
                    JOIN chats c ON c.conversation_id = m.conversation_id
                    WHERE {where}
                    ORDER BY m.sent_at DESC, m.conversation_id, m.message_id
                    LIMIT ? OFFSET ?""",
                [*parameters, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows], total

    @staticmethod
    def _fts_query(query: str, match_mode: str) -> str:
        if match_mode == "phrase":
            terms = [query.strip()]
        elif match_mode == "all_terms":
            terms = re.findall(r"\w+", query, flags=re.UNICODE)
            if not terms:
                raise ValueError("query must contain at least one searchable word.")
        else:
            raise ValueError("match_mode must be 'phrase' or 'all_terms'.")
        return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)

    def search_chats_by_participant(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        folded = query.casefold()
        with self._connect() as connection:
            total = connection.execute(
                """SELECT COUNT(DISTINCT p.conversation_id)
                   FROM participants p JOIN chats c USING(conversation_id)
                   WHERE instr(p.display_name_folded, ?) > 0""",
                (folded,),
            ).fetchone()[0]
            chat_rows = connection.execute(
                """SELECT c.conversation_id, c.display_name, c.chat_type,
                          c.last_message_at, c.last_message_preview, c.unread
                   FROM chats c WHERE c.conversation_id IN (
                     SELECT DISTINCT conversation_id FROM participants
                     WHERE instr(display_name_folded, ?) > 0
                   )
                   ORDER BY c.last_message_at DESC, c.conversation_id
                   LIMIT ? OFFSET ?""",
                (folded, limit, offset),
            ).fetchall()
            ids = [row["conversation_id"] for row in chat_rows]
            participant_rows = []
            if ids:
                placeholders = ",".join("?" for _ in ids)
                participant_rows = connection.execute(
                    f"""SELECT conversation_id, participant_id,
                              display_name AS participant_name
                       FROM participants WHERE conversation_id IN ({placeholders})
                         AND instr(display_name_folded, ?) > 0
                       ORDER BY conversation_id, display_name""",
                    [*ids, folded],
                ).fetchall()
        names_by_chat: dict[str, list[dict[str, str | None]]] = {}
        for row in participant_rows:
            names_by_chat.setdefault(row["conversation_id"], []).append(
                    {
                        "id": row["participant_id"] or None,
                        "display_name": row["participant_name"],
                    }
            )
        grouped: dict[str, dict[str, Any]] = {}
        for row in chat_rows:
            chat_id = row["conversation_id"]
            grouped[chat_id] = {
                "id": chat_id,
                "name": row["display_name"],
                "chat_type": row["chat_type"],
                "last_message_at": row["last_message_at"],
                "last_message_preview": row["last_message_preview"],
                "unread": None if row["unread"] is None else bool(row["unread"]),
                "matched_participants": names_by_chat.get(chat_id, []),
            }
        return list(grouped.values()), total

    def index_status(
        self,
        *,
        limit: int,
        offset: int,
        conversation_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        where = "WHERE c.conversation_id = ?" if conversation_id else ""
        parameters: tuple[Any, ...] = (conversation_id,) if conversation_id else ()
        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM chats c {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""SELECT c.conversation_id, c.display_name, c.last_message_at,
                          c.catalogued_at, s.messages_next_url,
                          s.message_history_complete, s.oldest_message_at,
                          s.newest_message_at, s.messages_indexed_at,
                          s.messages_stored, s.messages_scanned, s.message_pages,
                          s.missing_timestamp_messages, s.participant_sync_complete,
                          s.participant_sync_error, s.participants_indexed_at,
                          s.participants_scanned, s.unresolved_participants,
                          s.sync_attempted_at
                   FROM chats c LEFT JOIN chat_index_state s USING(conversation_id)
                   {where}
                   ORDER BY c.last_message_at DESC, c.conversation_id
                   LIMIT ? OFFSET ?""",
                (*parameters, limit, offset),
            ).fetchall()
            generation = int(
                connection.execute(
                    "SELECT value FROM index_metadata WHERE key = 'generation'"
                ).fetchone()[0]
            )
        return [dict(row) for row in rows], total, generation

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            return {
                "catalog_chats": connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0],
                "indexed_messages": connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "participant_rows": connection.execute("SELECT COUNT(*) FROM participants").fetchone()[0],
                "complete_message_histories": connection.execute(
                    "SELECT COUNT(*) FROM chat_index_state WHERE message_history_complete = 1"
                ).fetchone()[0],
                "complete_participant_rosters": connection.execute(
                    "SELECT COUNT(*) FROM chat_index_state WHERE participant_sync_complete = 1"
                ).fetchone()[0],
                "generation": int(connection.execute(
                    "SELECT value FROM index_metadata WHERE key = 'generation'"
                ).fetchone()[0]),
            }

    def participant_coverage(self) -> dict[str, int]:
        with self._connect() as connection:
            return {
                "catalog_chats": connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0],
                "complete_rosters": connection.execute(
                    "SELECT COUNT(*) FROM chat_index_state WHERE participant_sync_complete = 1 "
                    "AND unresolved_participants = 0"
                ).fetchone()[0],
                "synced_rosters": connection.execute(
                    "SELECT COUNT(*) FROM chat_index_state WHERE participant_sync_complete = 1"
                ).fetchone()[0],
                "rosters_with_unresolved_names": connection.execute(
                    "SELECT COUNT(*) FROM chat_index_state WHERE participant_sync_complete = 1 "
                    "AND unresolved_participants > 0"
                ).fetchone()[0],
                "unresolved_participants": connection.execute(
                    "SELECT COALESCE(SUM(unresolved_participants), 0) "
                    "FROM chat_index_state WHERE participant_sync_complete = 1"
                ).fetchone()[0],
                "indexed_participants": connection.execute(
                    "SELECT COUNT(*) FROM participants"
                ).fetchone()[0],
                "generation": int(connection.execute(
                    "SELECT value FROM index_metadata WHERE key = 'generation'"
                ).fetchone()[0]),
            }

    def coverage_summary(
        self,
        *,
        since: str | None,
        until: str | None,
        conversation_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        predicates = "WHERE c.conversation_id = ?" if conversation_id else ""
        params = (conversation_id,) if conversation_id else ()
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT c.conversation_id, c.display_name, c.last_message_at,
                          c.catalogued_at, s.message_history_complete,
                          s.oldest_message_at, s.newest_message_at,
                          s.messages_indexed_at, s.messages_scanned,
                          s.missing_timestamp_messages
                   FROM chats c LEFT JOIN chat_index_state s USING(conversation_id)
                   {predicates}
                   ORDER BY c.last_message_at DESC, c.conversation_id""",
                params,
            ).fetchall()
        records = []
        covered_count = 0
        for row_index, row in enumerate(rows):
            complete_history = bool(row["message_history_complete"] or 0)
            oldest = row["oldest_message_at"]
            newest = row["newest_message_at"]
            catalog_latest = row["last_message_at"]
            missing_timestamps = int(row["missing_timestamp_messages"] or 0)
            has_time_filter = since is not None or until is not None
            fresh_through_catalog = (
                newest is None and catalog_latest is None
            ) or bool(newest and catalog_latest and newest >= catalog_latest)
            range_covered = False
            if complete_history and not (has_time_filter and missing_timestamps):
                if not has_time_filter:
                    range_covered = fresh_through_catalog
                elif until is not None and newest is not None:
                    range_covered = until <= newest or bool(
                        fresh_through_catalog and catalog_latest and until >= catalog_latest
                    )
                elif since is not None:
                    range_covered = bool(oldest and since >= oldest and fresh_through_catalog)
            if not range_covered and since is not None and oldest and newest:
                lower_covered = since >= oldest
                upper_covered = (
                    until <= newest
                    if until is not None
                    else fresh_through_catalog
                )
                if until is not None and not upper_covered and fresh_through_catalog:
                    upper_covered = bool(catalog_latest and until >= catalog_latest)
                range_covered = lower_covered and upper_covered and not missing_timestamps
            if range_covered:
                covered_count += 1
            if offset <= row_index < offset + limit:
                records.append(
                    {
                        "chat_id": row["conversation_id"],
                        "chat_name": row["display_name"],
                        "messages_indexed": int(row["messages_scanned"] or 0),
                        "oldest_indexed_message_at": oldest,
                        "newest_indexed_message_at": newest,
                        "indexed_at": row["messages_indexed_at"],
                        "catalog_last_message_at": catalog_latest,
                        "catalog_refreshed_at": row["catalogued_at"],
                        "requested_since": since,
                        "requested_until": until,
                        "full_history_indexed": complete_history,
                        "requested_range_covered": range_covered,
                        "missing_timestamp_messages": missing_timestamps,
                    }
                )
        return {
            "catalog_chats": len(rows),
            "covered_chats": covered_count,
            "uncovered_chats": len(rows) - covered_count,
            "complete_for_requested_scope": covered_count == len(rows),
            "offset": offset,
            "returned": len(records),
            "items": records,
        }

    def _bump_generation(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE index_metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'"
        )

    @staticmethod
    def _message_author(message: dict[str, Any], body: str) -> str:
        for key in (
            "imdisplayname",
            "imDisplayName",
            "fromDisplayNameInToken",
            "displayName",
            "author",
        ):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "System" if body.lstrip().startswith(("<", "{")) else "Unknown sender"
