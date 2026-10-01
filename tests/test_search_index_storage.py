from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from teams_exporter.teams_connector import ChatSummary
from teams_exporter.teams_message_formatter import TeamsMessageFormatter
from teams_exporter.teams_search_index import TeamsSearchIndex
from teams_exporter.teams_service import TeamsService


def fingerprint(tenant_id: str, user_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{user_id}".encode("utf-8")).hexdigest()


class CachedIdentityConnector:
    def __init__(self, identity: dict[str, str]) -> None:
        self.identity = identity
        self.account_picture_uris = []

    def get_cached_account_metadata(self) -> dict[str, str]:
        return self.identity

    def get_profile_picture_uri(self, display_name: str | None = None) -> str | None:
        self.account_picture_uris.append(display_name)
        return None


class SearchIndexStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_archive_without_verified_owner_is_preserved_for_account_isolation(self):
        base = self.root / "search-index.sqlite3"
        unowned = TeamsSearchIndex(base)
        unowned.upsert_chats([ChatSummary("19:unowned@thread.v2", "Unowned")])

        selected, warning = TeamsSearchIndex.resolve_account_path(
            base, fingerprint("tenant-a", "user-a")
        )

        self.assertNotEqual(selected, base)
        self.assertIn("no verified account owner", warning)
        self.assertEqual(unowned.counts()["catalog_chats"], 1)
        self.assertFalse(selected.exists())

    def test_different_account_uses_a_separate_path_without_overwriting(self):
        base = self.root / "search-index.sqlite3"
        owner_a = fingerprint("tenant-a", "user-a")
        owner_b = fingerprint("tenant-b", "user-b")
        first = TeamsSearchIndex(base, owner_fingerprint=owner_a)
        first.upsert_chats([ChatSummary("19:private@thread.v2", "Private")])

        selected, warning = TeamsSearchIndex.resolve_account_path(base, owner_b)
        second = TeamsSearchIndex(selected, owner_fingerprint=owner_b)

        self.assertNotEqual(selected, base)
        self.assertIn("belongs to another account", warning)
        self.assertEqual(TeamsSearchIndex.read_owner_fingerprint(base), owner_a)
        self.assertEqual(TeamsSearchIndex.read_owner_fingerprint(selected), owner_b)
        self.assertEqual(first.counts()["catalog_chats"], 1)
        self.assertEqual(second.counts()["catalog_chats"], 0)

    def test_service_reopens_the_same_account_archive_and_separates_another_user(self):
        base = self.root / "custom-index.sqlite3"
        identity_a = {"tenant_id": "tenant-a", "user_id": "user-a"}
        identity_b = {"tenant_id": "tenant-b", "user_id": "user-b"}
        with patch.dict(os.environ, {"TEAMS_SEARCH_INDEX_PATH": str(base)}):
            first_service = TeamsService(connector=CachedIdentityConnector(identity_a))
            first_index = first_service.index
            first_index.upsert_chats([ChatSummary("19:alpha@thread.v2", "Alpha")])

            reopened_service = TeamsService(connector=CachedIdentityConnector(identity_a))
            reopened_index = reopened_service.index
            other_service = TeamsService(connector=CachedIdentityConnector(identity_b))
            other_index = other_service.index

        self.assertEqual(first_index.path, reopened_index.path)
        self.assertEqual(reopened_index.counts()["catalog_chats"], 1)
        self.assertNotEqual(first_index.path, other_index.path)
        self.assertEqual(other_index.counts()["catalog_chats"], 0)
        self.assertIn("belongs to another account", other_service.database_notice)
        self.assertEqual(TeamsSearchIndex.read_owner_fingerprint(first_index.path), fingerprint("tenant-a", "user-a"))

    def test_backup_is_consistent_portable_and_refuses_accidental_overwrite(self):
        owner = fingerprint("tenant-a", "user-a")
        source = TeamsSearchIndex(self.root / "active.sqlite3", owner_fingerprint=owner)
        source.upsert_chats([ChatSummary("19:alpha@thread.v2", "Alpha")])
        destination = self.root / "backup.sqlite3"

        created = source.backup_to(destination)
        backup = TeamsSearchIndex(created, owner_fingerprint=owner)

        self.assertEqual(backup.counts()["catalog_chats"], 1)
        self.assertEqual(TeamsSearchIndex.read_owner_fingerprint(destination), owner)
        with self.assertRaises(FileExistsError):
            source.backup_to(destination)

    def test_message_upsert_keeps_full_body_and_applies_only_newer_edits(self):
        index = TeamsSearchIndex(self.root / "messages.sqlite3")
        index.upsert_chats([ChatSummary("19:alpha@thread.v2", "Alpha")])
        formatter = TeamsMessageFormatter()

        def save(content: str, edit_time: str) -> None:
            index.upsert_messages(
                "19:alpha@thread.v2",
                [{
                    "id": "m1",
                    "originalarrivaltime": "2026-09-28T10:00:00Z",
                    "imdisplayname": "Taylor",
                    "messagetype": "Text",
                    "content": content,
                    "properties": {"edittime": edit_time},
                }],
                formatter=formatter,
            )

        save("first version", "2026-09-28T10:01:00Z")
        save("new edited body " + ("x" * 2_000), "2026-09-28T10:03:00Z")
        save("stale body", "2026-09-28T10:02:00Z")

        rows = index.query_message_page(
            "19:alpha@thread.v2", since=None, until=None, before=None, limit=10
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "new edited body " + ("x" * 2_000))
        self.assertEqual(rows[0]["edited_at"], "2026-09-28T10:03:00Z")

    def test_soft_delete_is_recorded_without_erasing_last_known_content(self):
        index = TeamsSearchIndex(self.root / "deleted.sqlite3")
        formatter = TeamsMessageFormatter()
        index.upsert_messages(
            "19:alpha@thread.v2",
            [{"id": "m1", "originalarrivaltime": "2026-09-28T10:00:00Z", "content": "kept locally"}],
            formatter=formatter,
        )
        index.upsert_messages(
            "19:alpha@thread.v2",
            [{
                "id": "m1",
                "originalarrivaltime": "2026-09-28T10:00:00Z",
                "messagetype": "MessageDelete",
                "content": "",
                "properties": {"deletetime": "2026-09-28T11:00:00Z"},
            }],
            formatter=formatter,
        )

        row = index.get_message("19:alpha@thread.v2", "m1")
        self.assertTrue(row["is_deleted"])
        self.assertEqual(row["deleted_at"], "2026-09-28T11:00:00Z")
        self.assertEqual(row["body"], "kept locally")

        page = index.query_message_page(
            "19:alpha@thread.v2", since=None, until=None, before=None, limit=10
        )
        self.assertEqual([item["message_id"] for item in page], ["m1"])
        self.assertTrue(page[0]["is_deleted"])
        self.assertEqual(page[0]["body"], "kept locally")

        matches, has_more = index.search_messages(
            "kept locally",
            match_mode="substring",
            conversation_ids=["19:alpha@thread.v2"],
            since="2026-09-28T09:00:00Z",
            until="2026-09-28T12:00:00Z",
            after=None,
            limit=10,
        )
        self.assertFalse(has_more)
        self.assertEqual([item["message_id"] for item in matches], ["m1"])
        self.assertTrue(matches[0]["is_deleted"])
        self.assertEqual(matches[0]["body"], "kept locally")

    def test_chat_delta_and_sync_token_persist_together(self):
        path = self.root / "catalog.sqlite3"
        index = TeamsSearchIndex(path)
        formatter = TeamsMessageFormatter()
        index.upsert_chat_updates(
            [ChatSummary(
                "19:channel@thread.tacv2",
                "Analytics · General",
                chat_type="channel",
                avatar_url="https://images.example.test/channel.png",
            )],
            "sanitized-sync-token",
        )
        index.upsert_messages(
            "19:channel@thread.tacv2",
            [{
                "id": "m1",
                "originalarrivaltime": "2026-09-28T10:00:00Z",
                "imdisplayname": "Taylor",
                "messagetype": "Text",
                "content": "retained channel message",
            }],
            formatter=formatter,
        )
        index.upsert_chat_updates([], "sanitized-next-sync-token", deleted_ids=["19:channel@thread.tacv2"])

        reopened = TeamsSearchIndex(path)
        catalog = reopened.chat_catalog()

        self.assertEqual(reopened.get_chat_sync_token(), "sanitized-next-sync-token")
        self.assertEqual(catalog, [])
        self.assertEqual(
            reopened.get_message("19:channel@thread.tacv2", "m1")["body"],
            "retained channel message",
        )

    def test_invalid_sync_token_does_not_write_a_partial_catalog(self):
        index = TeamsSearchIndex(self.root / "catalog-invalid.sqlite3")

        with self.assertRaises(ValueError):
            index.upsert_chat_updates(
                [ChatSummary("19:alpha@thread.v2", "Alpha")],
                " ",
            )

        self.assertEqual(index.chat_catalog(), [])
        self.assertIsNone(index.get_chat_sync_token())


if __name__ == "__main__":
    unittest.main()
