from __future__ import annotations

from pathlib import Path

import pytest

from teams_connector.domain.errors import AccountMismatch
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


