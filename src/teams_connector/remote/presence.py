"""Teams presence (Unified Presence, regional): availability, activity, last-active time and device.

The lookup is a read: a POST of the person's MRI, sent with the ``teams.spaces`` bearer to the regional host
``{region}.presence.teams.microsoft.com``. The region is the first label of the Chat Service host from AuthZ.
Only the fields the connector reports are kept: calendar data, work location, forced availability and
capabilities are dropped here. The live check (2026-10-15) covered one region; the flat host redirects to it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from teams_connector.domain.errors import Upstream
from teams_connector.domain.models import Presence
from teams_connector.remote.chat_service_mapping import parse_time
from teams_connector.remote.teams_session import TeamsSession
from teams_connector.remote.transport import Credential, Transport, operation


class TeamsPresence:
    """PresenceReader over the regional Unified Presence service."""

    def __init__(self, transport: Transport, session: TeamsSession, credential: Credential) -> None:
        self._transport = transport
        self._session = session
        self._credential = credential

    async def presence(self, user_id: str) -> Presence | None:
        """Entry point: the presence of one person by Entra object id. The id is only sent as its MRI."""
        info = await self._session.info()
        region = (urlsplit(info.chat_service_url).hostname or "").split(".")[0]
        if not region:
            raise Upstream("No regional Chat Service host to read presence from.")
        url = f"https://{region}.presence.teams.microsoft.com/v1/presence/getpresence"
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
    """The first person's presence in the response, with only the reported fields."""
    rows = data if isinstance(data, list) else []
    row = next((item for item in rows if isinstance(item, dict)), None)
    presence = row.get("presence") if row else None
    if not isinstance(presence, dict):
        return None
    return Presence(
        availability=_text(presence.get("availability")),
        activity=_text(presence.get("activity")),
        last_active_at=parse_time(presence.get("lastActiveTime")),
        device_type=_text(presence.get("deviceType")),
    )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
