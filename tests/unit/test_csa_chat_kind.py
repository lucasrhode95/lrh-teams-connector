"""CSA chat kind: an explicit type decides it, so a meeting stays a meeting when the row is not one-on-one.

Synthetic rows only.
"""

from __future__ import annotations

import pytest

from teams_connector.remote.csa_mapping import Me, chat_kind

ME = Me(user_id="11111111-1111-1111-1111-111111111111", name="Synthetic Me")


@pytest.mark.parametrize(
    ("row", "kind"),
    [
        ({"chatType": "meeting", "isOneOnOne": False, "threadProperties": {}}, "meeting"),
        ({"chatType": "group", "isOneOnOne": False, "threadProperties": {}}, "group"),
        ({"chatType": "oneOnOne", "isOneOnOne": True, "threadProperties": {}}, "one_on_one"),
    ],
)
def test_explicit_chat_type_decides_the_kind(row: dict[str, object], kind: str) -> None:
    assert chat_kind(row, ME) == kind
