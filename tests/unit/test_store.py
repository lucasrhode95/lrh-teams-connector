from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from teams_connector.domain.errors import AccountMismatch
from teams_connector.domain.models import Chat
from teams_connector.store.db import Store, store_path


def test_archive_is_bound_to_its_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMS_CONNECTOR_HOME", str(tmp_path / "connector"))
    first_path = store_path("account-one")
    second_path = store_path("account-two")
    assert first_path != second_path

    store = Store(first_path, "account-one")
    store.save_catalog([Chat(id="chat-one", name="Synthetic chat", kind="group")])

    reopened = Store(first_path, "account-one")
    assert reopened.chat("chat-one") is not None
    with pytest.raises(AccountMismatch, match="different Microsoft account"):
        Store(first_path, "account-two")


def test_a_message_archive_from_an_earlier_version_is_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEAMS_CONNECTOR_HOME", str(tmp_path / "connector"))
    path = store_path("account-one")
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta (key, value) VALUES ('owner', 'account-one');"
            "CREATE TABLE messages (id TEXT PRIMARY KEY, data TEXT);"
            "INSERT INTO messages (id, data) VALUES ('synthetic-message', 'Synthetic content');"
        )
        db.commit()

    store = Store(path, "account-one")

    assert store.chats() == []
    moved = [item for item in path.parent.iterdir() if item.name.startswith("legacy-archive-")]
    assert len(moved) == 1 and (moved[0] / "teams.sqlite3").exists()
