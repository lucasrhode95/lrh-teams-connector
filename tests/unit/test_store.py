from __future__ import annotations

from pathlib import Path

import pytest

from teams_connector.domain.errors import AccountMismatch, InvalidRequest
from teams_connector.domain.models import Chat, Message
from teams_connector.store.db import Store, store_path


def test_archive_is_bound_to_its_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMS_CONNECTOR_HOME", str(tmp_path / "connector"))
    first_path = store_path("account-one")
    second_path = store_path("account-two")
    assert first_path != second_path

    store = Store(first_path, "account-one")
    store.save_catalog([Chat(id="chat-one", name="Synthetic chat", kind="group")])
    store.save_messages(
        "chat-one",
        [Message(id="message-one", conversation_id="chat-one", text="Synthetic content")],
    )

    reopened = Store(first_path, "account-one")
    assert reopened.chat("chat-one") is not None
    assert reopened.message("chat-one", "message-one").text == "Synthetic content"  # type: ignore[union-attr]
    with pytest.raises(AccountMismatch, match="different Microsoft account"):
        Store(first_path, "account-two")


def test_backup_is_consistent_and_never_overwrites_without_consent(tmp_path: Path) -> None:
    source = Store(tmp_path / "live.sqlite3", "account-one")
    source.save_catalog([Chat(id="chat-one", name="Synthetic chat", kind="group")])
    source.save_messages(
        "chat-one",
        [Message(id="message-one", conversation_id="chat-one", text="Synthetic content")],
    )
    destination = tmp_path / "backup.sqlite3"

    assert source.backup_to(destination) == destination
    backup = Store(destination, "account-one")
    assert backup.message("chat-one", "message-one").text == "Synthetic content"  # type: ignore[union-attr]

    with pytest.raises(InvalidRequest, match="already exists"):
        source.backup_to(destination)
    assert source.backup_to(destination, overwrite=True) == destination
    with pytest.raises(InvalidRequest, match="live archive itself"):
        source.backup_to(source.path)
