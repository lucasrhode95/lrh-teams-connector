"""Teams presence (Unified Presence, regional): availability, activity, last-active time and device.

The lookup is a read: a POST of the person's MRI, sent with the ``teams.spaces`` bearer to the presence
service AuthZ names for the account (``regionGtms.presenceUPS``, such as ``https://teams.microsoft.com/ups/emea``),
as Teams Web does. Only the fields the connector reports are kept: calendar data, work location, forced
availability and capabilities are dropped here. Checked live on 2026-10-15 in one region.
"""

from __future__ import annotations

from typing import Any

from teams_connector.domain.errors import Upstream
from teams_connector.domain.models import Presence
from teams_connector.remote.chat_service_mapping import parse_time
from teams_connector.remote.teams_session import TeamsSession
from teams_connector.remote.transport import Credential, Transport, operation

STATUS_OK = 20000  # a real reading; others include 40401 user not found and 50002 federation failed


class TeamsPresence:
    """PresenceReader over the regional Unified Presence service."""

    def __init__(self, transport: Transport, session: TeamsSession, credential: Credential) -> None:
        self._transport = transport
        self._session = session
        self._credential = credential

    async def presence(self, user_id: str) -> Presence | None:
        """Entry point: the presence of one person by Entra object id. The id is only sent as its MRI."""
        info = await self._session.info()
        if info.presence_url is None:
            raise Upstream("Teams AuthZ named no presence service for this account.")
        url = f"{info.presence_url}/v1/presence/getpresence/"
        with operation("reading presence"):
            data = await self._transport.json(
                "POST",
                url,
                credential=self._credential,
                json_body=[{"mri": f"8:orgid:{user_id}"}],
                retry=True,
            )
        return _presence_of(data)


def _presence_of(data: Any) -> Presence | None:
    """The first person's presence in the response, with only the reported fields. A row whose status is
    not OK (such as 40401, a user the service does not know) carries a placeholder Offline, not a real
    status, so it counts as no presence."""
    rows = data if isinstance(data, list) else []
    row = next((item for item in rows if isinstance(item, dict)), None)
    presence = row.get("presence") if row else None
    if not isinstance(presence, dict) or (row or {}).get("status") != STATUS_OK:
        return None
    return Presence(
        availability=_text(presence.get("availability")),
        activity=_text(presence.get("activity")),
        last_active_at=parse_time(presence.get("lastActiveTime")),
        device_type=_text(presence.get("deviceType")),
    )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
