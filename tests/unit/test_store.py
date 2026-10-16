from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from teams_connector.domain.errors import AccountMismatch
from teams_connector.domain.models import Chat
from teams_connector.store.db import Store, store_path


def test_store_is_bound_to_its_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_store_lists_chats_by_latest_activity_with_unknown_activity_last(tmp_path: Path) -> None:
    store = Store(tmp_path / "catalog.sqlite3", "synthetic-account")
    store.save_catalog(
        [
            Chat(
                id="older",
                name="Older",
                kind="group",
                last_message_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            Chat(
                id="latest",
                name="Latest",
                kind="group",
                last_message_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
            Chat(id="unknown", name="Unknown", kind="group"),
        ]
    )

    assert [chat.id for chat in store.chats()] == ["latest", "older", "unknown"]
