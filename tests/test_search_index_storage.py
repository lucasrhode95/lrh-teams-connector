from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from teams_exporter.teams_connector import ChatSummary
from teams_exporter.teams_search_index import TeamsSearchIndex
from teams_exporter.teams_service import TeamsService


def fingerprint(tenant_id: str, user_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{user_id}".encode("utf-8")).hexdigest()


class CachedIdentityConnector:
    def __init__(self, identity: dict[str, str]) -> None:
        self.identity = identity

    def get_cached_account_metadata(self) -> dict[str, str]:
        return self.identity


class SearchIndexStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_legacy_archive_is_preserved_and_account_gets_separate_archive(self):
        base = self.root / "search-index.sqlite3"
        legacy = TeamsSearchIndex(base)
        legacy.upsert_chats([ChatSummary("19:legacy@thread.v2", "Legacy")])

        selected, warning = TeamsSearchIndex.resolve_account_path(
            base, fingerprint("tenant-a", "user-a")
        )

        self.assertNotEqual(selected, base)
        self.assertIn("no verified account owner", warning)
        self.assertEqual(legacy.counts()["catalog_chats"], 1)
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


if __name__ == "__main__":
    unittest.main()
