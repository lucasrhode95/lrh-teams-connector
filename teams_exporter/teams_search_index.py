from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from teams_exporter.teams_connector import ChatSummary, TeamsConnector
from teams_exporter.teams_message_formatter import TeamsMessageFormatter


class DatabaseOwnerMismatchError(RuntimeError):
    """Raised when an index path is already bound to another Teams account."""


class TeamsSearchIndex:
    """Local SQLite chat catalog, retained message archive, and search coverage."""

    OWNER_METADATA_KEY = "account_fingerprint"
    EMPTY_STATE_BOOTSTRAP_KEY = "csa_empty_state_bootstrapped"

    def __init__(
        self,
        path: str | Path = ".teams_search_index.sqlite3",
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
                        avatar_url TEXT,
                        is_empty_conversation INTEGER,
                        is_deleted INTEGER NOT NULL DEFAULT 0,
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
                        body_folded TEXT NOT NULL DEFAULT '',
                        message_type TEXT,
                        edited_at TEXT,
                        deleted_at TEXT,
                        is_deleted INTEGER NOT NULL DEFAULT 0,
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
                    CREATE TABLE IF NOT EXISTS export_jobs (
                        export_id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        since TEXT NOT NULL,
                        until TEXT NOT NULL,
                        message_limit INTEGER,
                        next_url TEXT,
                        complete INTEGER NOT NULL DEFAULT 0,
                        pages INTEGER NOT NULL DEFAULT 0,
                        scanned INTEGER NOT NULL DEFAULT 0,
                        missing_timestamps INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        snapshot_path TEXT,
                        byte_size INTEGER,
                        message_count INTEGER,
                        seen_urls_json TEXT NOT NULL DEFAULT '[]',
                        FOREIGN KEY (conversation_id) REFERENCES chats(conversation_id)
                            ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS export_job_messages (
                        export_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        PRIMARY KEY (export_id, message_id),
                        FOREIGN KEY (export_id) REFERENCES export_jobs(export_id)
                            ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS index_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT OR IGNORE INTO index_metadata(key, value)
                        VALUES ('generation', '0');
                    """
                )
                chat_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(chats)")
                }
                if "is_empty_conversation" not in chat_columns:
                    connection.execute(
                        "ALTER TABLE chats ADD COLUMN is_empty_conversation INTEGER"
                    )
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
        except sqlite3.OperationalError:
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
            self._upsert_chats(connection, chats, now)

    def upsert_chat_updates(
        self,
        chats: list[ChatSummary],
        sync_token: str,
        deleted_ids: list[str] | None = None,
        catalog_bootstrap: bool = False,
    ) -> None:
        """Atomically merge a CSA delta and persist its continuation token."""
        if not isinstance(sync_token, str) or not sync_token.strip():
            raise ValueError("The Teams chat update response has no sync token.")
        if not isinstance(catalog_bootstrap, bool):
            raise ValueError("catalog_bootstrap must be a boolean.")
        now = self._now()
        deleted_ids = deleted_ids or []
        if any(not isinstance(value, str) or not value for value in deleted_ids):
            raise ValueError("deleted_ids must contain non-empty conversation IDs.")
        with self._connect() as connection:
            self._upsert_chats(connection, chats, now)
            connection.executemany(
                "UPDATE chats SET is_deleted = 1, catalogued_at = ? WHERE conversation_id = ?",
                ((now, conversation_id) for conversation_id in deleted_ids),
            )
            connection.execute(
                """INSERT INTO index_metadata(key, value) VALUES ('csa_sync_token', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (sync_token,),
            )
            if catalog_bootstrap:
                connection.execute(
                    """INSERT INTO index_metadata(key, value) VALUES (?, '1')
                       ON CONFLICT(key) DO UPDATE SET value = '1'""",
                    (self.EMPTY_STATE_BOOTSTRAP_KEY,),
                )

    def has_csa_empty_state_bootstrap(self) -> bool:
        """Whether a full catalog response has populated empty-chat metadata."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM index_metadata WHERE key = ?",
                (self.EMPTY_STATE_BOOTSTRAP_KEY,),
            ).fetchone()
        return bool(row and row["value"] == "1")

    def get_chat_sync_token(self) -> str | None:
        """Return this account database's opaque CSA continuation token."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM index_metadata WHERE key = 'csa_sync_token'"
            ).fetchone()
        return str(row["value"]) if row else None

    def chat_catalog(self) -> list[dict[str, Any]]:
        """Read the durable catalog in descending activity order."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT conversation_id, display_name, chat_type, last_message_at,
                          last_message_preview, unread, avatar_url,
                          is_empty_conversation
                   FROM chats
                   WHERE is_deleted = 0
                   ORDER BY last_message_at IS NULL, last_message_at DESC, rowid"""
            ).fetchall()
        return [dict(row) for row in rows]

    @classmethod
    def _upsert_chats(
        cls,
        connection: sqlite3.Connection,
        chats: list[ChatSummary],
        now: str,
    ) -> None:
        for chat in chats:
            connection.execute(
                """INSERT INTO chats (
                    conversation_id, display_name, chat_type, last_message_at,
                    last_message_preview, unread, avatar_url,
                    is_empty_conversation, catalogued_at, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    display_name = CASE
                        WHEN excluded.display_name = excluded.conversation_id
                             AND chats.display_name <> chats.conversation_id
                        THEN chats.display_name
                        ELSE excluded.display_name
                    END,
                    chat_type = COALESCE(excluded.chat_type, chats.chat_type),
                    last_message_at = COALESCE(excluded.last_message_at, chats.last_message_at),
                    last_message_preview = COALESCE(excluded.last_message_preview, chats.last_message_preview),
                    unread = COALESCE(excluded.unread, chats.unread),
                    avatar_url = COALESCE(excluded.avatar_url, chats.avatar_url),
                    is_empty_conversation = COALESCE(
                        excluded.is_empty_conversation,
                        chats.is_empty_conversation
                    ),
                    is_deleted = 0,
                    catalogued_at = excluded.catalogued_at""",
                (
                    chat.id,
                    chat.display_name,
                    chat.chat_type,
                    cls._timestamp(chat.last_message_at),
                    chat.last_message_preview,
                    None if chat.unread is None else int(chat.unread),
                    chat.avatar_url,
                    None
                    if chat.is_empty_conversation is None
                    else int(chat.is_empty_conversation),
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
            stored, oldest, newest, _message_ids = self._upsert_message_rows(
                connection,
                conversation_id,
                messages,
                formatter=formatter,
                now=now,
                oldest=oldest,
                newest=newest,
            )

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

    def upsert_messages(
        self,
        conversation_id: str,
        messages: list[dict[str, Any]],
        formatter: TeamsMessageFormatter,
    ) -> dict[str, int]:
        """Upsert one fresh remote page without changing full-history progress."""
        now = self._now()
        with self._connect() as connection:
            prior = connection.execute(
                "SELECT oldest_message_at, newest_message_at FROM chat_index_state "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            stored, oldest, newest, _message_ids = self._upsert_message_rows(
                connection,
                conversation_id,
                messages,
                formatter=formatter,
                now=now,
                oldest=prior["oldest_message_at"] if prior else None,
                newest=prior["newest_message_at"] if prior else None,
            )
            connection.execute(
                """INSERT INTO chat_index_state (
                       conversation_id, oldest_message_at, newest_message_at,
                       messages_indexed_at
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       oldest_message_at = excluded.oldest_message_at,
                       newest_message_at = excluded.newest_message_at,
                       messages_indexed_at = excluded.messages_indexed_at""",
                (conversation_id, oldest, newest, now),
            )
            self._bump_generation(connection)
        return {"messages_added": stored, "messages_received": len(messages)}

    def _upsert_message_rows(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        messages: list[dict[str, Any]],
        formatter: TeamsMessageFormatter,
        now: str,
        oldest: str | None,
        newest: str | None,
    ) -> tuple[int, str | None, str | None, list[str]]:
        connection.execute(
            """INSERT OR IGNORE INTO chats(conversation_id, display_name, catalogued_at)
               VALUES (?, ?, ?)""",
            (conversation_id, conversation_id, now),
        )
        inserted = 0
        message_ids = []
        for message in messages:
            body = formatter.format_message(message)
            author = self._message_author(message, body)
            author_id = TeamsConnector._message_author_id(message)
            timestamp = self._timestamp(TeamsConnector.message_datetime(message))
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
            message_ids.append(message_id)

            message_type = message.get("messagetype") or message.get("messageType")
            if not isinstance(message_type, str):
                message_type = None
            properties = self._message_properties(message)
            edited_at = self._timestamp_value(
                properties.get("edittime") or message.get("edittime")
            )
            deleted_at = self._timestamp_value(
                properties.get("deletetime") or message.get("deletetime")
            )
            is_deleted = bool(
                (message_type or "").casefold() == "messagedelete" or deleted_at
            )
            existing = connection.execute(
                """SELECT row_id, body, edited_at, deleted_at, is_deleted
                   FROM messages WHERE conversation_id = ? AND message_id = ?""",
                (conversation_id, message_id),
            ).fetchone()
            if existing:
                previous_edit = self._revision_key(existing["edited_at"])
                previous_delete = self._revision_key(existing["deleted_at"])
                incoming_edit = self._revision_key(edited_at)
                if is_deleted:
                    body = str(existing["body"])
                    timestamp = None
                    author = None
                    author_id = None
                    message_type = None
                    edited_at = existing["edited_at"] or edited_at
                    deleted_at = max(
                        (value for value in (existing["deleted_at"], deleted_at) if value),
                        default=None,
                    )
                elif existing["is_deleted"]:
                    if incoming_edit and incoming_edit > max(previous_delete, previous_edit):
                        # A later explicit edit is evidence that this ID became active again.
                        deleted_at = None
                    else:
                        # A plain historical row can be stale; absence of a tombstone
                        # in that row is not evidence that the message was restored.
                        body = str(existing["body"])
                        timestamp = None
                        author = None
                        author_id = None
                        message_type = None
                        edited_at = existing["edited_at"]
                        deleted_at = existing["deleted_at"]
                        is_deleted = True
                elif previous_edit and (not incoming_edit or incoming_edit < previous_edit):
                    # Never allow an older/unversioned page to replace known edited text.
                    body = str(existing["body"])
                    timestamp = None
                    author = None
                    author_id = None
                    message_type = None
                    edited_at = existing["edited_at"]
                    deleted_at = existing["deleted_at"]
                    is_deleted = bool(existing["is_deleted"])
            folded = self._normalize_search_text(body)

            if existing:
                row_id = int(existing["row_id"])
                connection.execute(
                    """UPDATE messages SET
                           author = COALESCE(?, author),
                           author_id = COALESCE(?, author_id),
                           sent_at = COALESCE(?, sent_at),
                           body = ?, body_folded = ?,
                           message_type = COALESCE(?, message_type),
                           edited_at = COALESCE(?, edited_at),
                           deleted_at = ?,
                           is_deleted = ?, indexed_at = ?
                       WHERE row_id = ?""",
                    (
                        author,
                        author_id,
                        timestamp,
                        body,
                        folded,
                        message_type,
                        edited_at,
                        deleted_at,
                        int(is_deleted),
                        now,
                        row_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """INSERT INTO messages (
                           conversation_id, message_id, author, author_id, sent_at,
                           body, body_folded, message_type, edited_at, deleted_at,
                           is_deleted, indexed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        conversation_id,
                        message_id,
                        author,
                        author_id,
                        timestamp,
                        body,
                        folded,
                        message_type,
                        edited_at,
                        deleted_at,
                        int(is_deleted),
                        now,
                    ),
                )
                row_id = int(cursor.lastrowid)
                inserted += 1
            if timestamp:
                oldest = timestamp if oldest is None else min(oldest, timestamp)
                newest = timestamp if newest is None else max(newest, timestamp)
        return inserted, oldest, newest, message_ids

    @staticmethod
    def _message_properties(message: dict[str, Any]) -> dict[str, Any]:
        properties = message.get("properties")
        if isinstance(properties, dict):
            return properties
        if isinstance(properties, str):
            try:
                decoded = json.loads(properties)
            except json.JSONDecodeError:
                return {}
            return decoded if isinstance(decoded, dict) else {}
        return {}

    @classmethod
    def _timestamp_value(cls, value: Any) -> str | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
            except (ValueError, OverflowError, OSError):
                return None
        if isinstance(value, str) and value.strip():
            if value.strip().isdigit():
                try:
                    return datetime.fromtimestamp(
                        int(value.strip()) / 1000, timezone.utc
                    ).isoformat().replace("+00:00", "Z")
                except (ValueError, OverflowError, OSError):
                    return None
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return value.strip()
            return cls._timestamp(parsed)
        return None

    @classmethod
    def _revision_key(cls, value: Any) -> str:
        return cls._timestamp_value(value) or ""

    def query_message_page(
        self,
        conversation_id: str,
        since: str | None,
        until: str | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        predicates = ["m.conversation_id = ?", "m.sent_at IS NOT NULL"]
        parameters: list[Any] = [conversation_id]
        if since is not None:
            predicates.append("m.sent_at >= ?")
            parameters.append(since)
        if until is not None:
            predicates.append("m.sent_at <= ?")
            parameters.append(until)
        if before is not None:
            predicates.append(
                "(m.sent_at < ? OR (m.sent_at = ? AND m.message_id < ?))"
            )
            parameters.extend((before[0], before[0], before[1]))
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT m.conversation_id, m.message_id, m.author, m.author_id,
                           m.sent_at, m.body, m.message_type, m.edited_at,
                           m.deleted_at, m.is_deleted, c.display_name
                    FROM messages m JOIN chats c USING(conversation_id)
                    WHERE {' AND '.join(predicates)}
                    ORDER BY m.sent_at DESC, m.message_id DESC
                    LIMIT ?""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def query_messages_chronological(
        self,
        conversation_id: str,
        since: str | None,
        until: str | None,
        after: tuple[str, str] | None = None,
        from_key: tuple[str, str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        predicates = ["conversation_id = ?", "sent_at IS NOT NULL"]
        parameters: list[Any] = [conversation_id]
        if since is not None:
            predicates.append("sent_at >= ?")
            parameters.append(since)
        if until is not None:
            predicates.append("sent_at <= ?")
            parameters.append(until)
        if after is not None:
            predicates.append("(sent_at, message_id) > (?, ?)")
            parameters.extend(after)
        if from_key is not None:
            predicates.append("(sent_at, message_id) >= (?, ?)")
            parameters.extend(from_key)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT conversation_id, message_id, author, author_id, sent_at,
                           body, message_type, edited_at, deleted_at, is_deleted
                    FROM messages WHERE {' AND '.join(predicates)}
                    ORDER BY sent_at ASC, message_id ASC LIMIT ?""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_messages_in_range(
        self, conversation_id: str, *, since: str | None, until: str | None
    ) -> int:
        predicates = ["conversation_id = ?", "sent_at IS NOT NULL"]
        parameters: list[Any] = [conversation_id]
        if since is not None:
            predicates.append("sent_at >= ?")
            parameters.append(since)
        if until is not None:
            predicates.append("sent_at <= ?")
            parameters.append(until)
        with self._connect() as connection:
            return int(connection.execute(
                f"SELECT COUNT(*) FROM messages WHERE {' AND '.join(predicates)}",
                parameters,
            ).fetchone()[0])

    def latest_message_cutoff(
        self,
        conversation_id: str,
        since: str | None,
        until: str | None,
        limit: int,
    ) -> tuple[str, str] | None:
        if limit < 1:
            return None
        predicates = ["conversation_id = ?", "sent_at IS NOT NULL"]
        parameters: list[Any] = [conversation_id]
        if since is not None:
            predicates.append("sent_at >= ?")
            parameters.append(since)
        if until is not None:
            predicates.append("sent_at <= ?")
            parameters.append(until)
        parameters.append(limit - 1)
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT sent_at, message_id FROM messages
                    WHERE {' AND '.join(predicates)}
                    ORDER BY sent_at DESC, message_id DESC LIMIT 1 OFFSET ?""",
                parameters,
            ).fetchone()
        return (str(row["sent_at"]), str(row["message_id"])) if row else None

    def get_message(self, conversation_id: str, message_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT conversation_id, message_id, author, author_id, sent_at,
                          body, message_type, edited_at, deleted_at, is_deleted
                   FROM messages WHERE conversation_id = ? AND message_id = ?""",
                (conversation_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def create_export_job(
        self,
        export_id: str,
        conversation_id: str,
        since: str,
        until: str,
        message_limit: int | None,
    ) -> dict[str, Any]:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO chats(conversation_id, display_name, catalogued_at)
                   VALUES (?, ?, ?) ON CONFLICT(conversation_id) DO NOTHING""",
                (conversation_id, conversation_id, now),
            )
            connection.execute(
                """INSERT INTO export_jobs(
                       export_id, conversation_id, since, until, message_limit,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (export_id, conversation_id, since, until, message_limit, now, now),
            )
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE export_id = ?", (export_id,)
            ).fetchone()
        return dict(row)

    def get_export_job(self, export_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE export_id = ?", (export_id,)
            ).fetchone()
        return dict(row) if row else None

    def count_export_job_messages(self, export_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM export_job_messages WHERE export_id = ?",
                (export_id,),
            ).fetchone()
        return int(row[0])

    def save_export_job_page(
        self,
        export_id: str,
        messages: list[dict[str, Any]],
        requested_url: str | None,
        next_url: str | None,
        raw_scanned: int,
        missing_timestamp_count: int,
        formatter: TeamsMessageFormatter,
    ) -> dict[str, Any]:
        now = self._now()
        with self._connect() as connection:
            job = connection.execute(
                "SELECT * FROM export_jobs WHERE export_id = ?", (export_id,)
            ).fetchone()
            if job is None:
                raise KeyError("Export ID was not found.")
            seen_urls = json.loads(job["seen_urls_json"] or "[]")
            if requested_url and requested_url in seen_urls:
                raise RuntimeError("Teams repeated an export continuation link.")
            if requested_url:
                seen_urls.append(requested_url)
            chat_state = connection.execute(
                "SELECT oldest_message_at, newest_message_at FROM chat_index_state "
                "WHERE conversation_id = ?",
                (job["conversation_id"],),
            ).fetchone()
            _, oldest, newest, message_ids = self._upsert_message_rows(
                connection,
                str(job["conversation_id"]),
                messages,
                formatter=formatter,
                now=now,
                oldest=chat_state["oldest_message_at"] if chat_state else None,
                newest=chat_state["newest_message_at"] if chat_state else None,
            )
            message_limit = job["message_limit"]
            retrieved_count = 0
            if message_limit is not None:
                connection.executemany(
                    "INSERT OR IGNORE INTO export_job_messages(export_id, message_id) "
                    "VALUES (?, ?)",
                    ((export_id, message_id) for message_id in message_ids),
                )
                retrieved_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM export_job_messages WHERE export_id = ?",
                        (export_id,),
                    ).fetchone()[0]
                )
            limit_reached = (
                message_limit is not None and retrieved_count >= int(message_limit)
            )
            effective_next_url = None if limit_reached else next_url
            if effective_next_url and effective_next_url in seen_urls:
                raise RuntimeError("Teams repeated an export continuation link.")
            connection.execute(
                """INSERT INTO chat_index_state(
                       conversation_id, oldest_message_at, newest_message_at,
                       messages_indexed_at
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       oldest_message_at = excluded.oldest_message_at,
                       newest_message_at = excluded.newest_message_at,
                       messages_indexed_at = excluded.messages_indexed_at""",
                (job["conversation_id"], oldest, newest, now),
            )
            connection.execute(
                """UPDATE export_jobs SET next_url = ?, complete = ?,
                       pages = pages + 1, scanned = scanned + ?,
                       missing_timestamps = missing_timestamps + ?,
                       seen_urls_json = ?, updated_at = ?
                   WHERE export_id = ?""",
                (
                    effective_next_url,
                    int(effective_next_url is None),
                    raw_scanned,
                    missing_timestamp_count,
                    json.dumps(seen_urls, separators=(",", ":")),
                    now,
                    export_id,
                ),
            )
            self._bump_generation(connection)
            updated = connection.execute(
                "SELECT * FROM export_jobs WHERE export_id = ?", (export_id,)
            ).fetchone()
        return dict(updated)

    def complete_export_job(
        self,
        export_id: str,
        snapshot_path: str,
        byte_size: int,
        message_count: int,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """UPDATE export_jobs SET snapshot_path = ?, byte_size = ?,
                       message_count = ?, updated_at = ? WHERE export_id = ?""",
                (snapshot_path, byte_size, message_count, self._now(), export_id),
            )
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE export_id = ?", (export_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Export ID was not found.")
        return dict(row)

    def save_participants(
        self,
        conversation_id: str,
        participants: list[dict[str, str | None]],
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

    @staticmethod
    def _normalize_search_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return " ".join(normalized.split())

    @staticmethod
    def _like_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def search_messages(
        self,
        query: str,
        match_mode: str,
        conversation_ids: list[str],
        since: str,
        until: str,
        after: tuple[str, str, str] | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Search normalized SQL text and return one stable keyset page."""
        normalized = self._normalize_search_text(query)
        predicates = ["m.sent_at IS NOT NULL"]
        parameters: list[Any] = []
        placeholders = ",".join("?" for _ in conversation_ids)
        predicates.append(f"m.conversation_id IN ({placeholders})")
        parameters.extend(conversation_ids)
        predicates.extend(["m.sent_at >= ?", "m.sent_at <= ?"])
        parameters.extend((since, until))
        if match_mode == "exact":
            predicates.append("m.body_folded = ?")
            parameters.append(normalized)
        elif match_mode == "substring":
            predicates.append("m.body_folded LIKE ? ESCAPE '\\'")
            parameters.append("%" + self._like_literal(normalized) + "%")
        elif match_mode == "all_terms":
            terms = re.findall(r"\w+", normalized, flags=re.UNICODE)
            if not terms:
                raise ValueError("query must contain at least one searchable word.")
            for term in terms:
                predicates.append("m.body_folded LIKE ? ESCAPE '\\'")
                parameters.append("%" + self._like_literal(term) + "%")
        else:
            raise ValueError("match_mode must be 'exact', 'substring', or 'all_terms'.")
        if after is not None:
            predicates.append(
                "(m.sent_at, m.conversation_id, m.message_id) < (?, ?, ?)"
            )
            parameters.extend(after)
        parameters.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT m.conversation_id, m.message_id, m.author, m.author_id,
                           m.sent_at, m.body, m.message_type, m.edited_at,
                           m.deleted_at, m.is_deleted, c.display_name
                    FROM messages m JOIN chats c USING(conversation_id)
                    WHERE {' AND '.join(predicates)}
                    ORDER BY m.sent_at DESC, m.conversation_id DESC, m.message_id DESC
                    LIMIT ?""",
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        return [dict(row) for row in rows[:limit]], has_more

    def search_chats_by_participant(
        self,
        query: str,
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
