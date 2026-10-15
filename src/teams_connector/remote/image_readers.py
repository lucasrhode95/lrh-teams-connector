"""Pasted-image downloads: AMS full-size views, and Graph's hosted copies of channel images.

Each reader fetches only the URL a message named, from that URL's own allowlisted host: AMS with the session's
Skype token, Graph with the Graph token. Nothing is listed, searched or written beyond the destination file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from teams_connector.domain.errors import InvalidRequest
from teams_connector.remote.graph import GRAPH_ROOT, Graph
from teams_connector.remote.ports import RemoteFile
from teams_connector.remote.teams_session import TeamsSession
from teams_connector.remote.transport import Transport


class AmsCredential:
    """AMS image authentication: the session's Skype token as ``Authorization: skype_token``."""

    def __init__(self, session: TeamsSession) -> None:
        self._session = session

    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        info = await self._session.info(renew=renewal)
        return {"Authorization": f"skype_token {info.skype_token}"}

    def sign_in_command(self) -> str:
        return self._session.sign_in_command()


class AmsImages:
    """ImageReader over AMS: a pasted image's full-size view, authenticated with the session's Skype token."""

    def __init__(self, transport: Transport, credential: AmsCredential) -> None:
        self._transport = transport
        self._credential = credential

    async def download(self, url: str, dest: Path, *, max_bytes: int) -> RemoteFile:
        if not url.startswith("https://"):
            raise InvalidRequest("Only HTTPS image URLs can be downloaded.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            content_type, size = await self._transport.download(
                url, handle, credential=self._credential, max_bytes=max_bytes
            )
        return RemoteFile(name=None, content_type=content_type, size=size)


class GraphHostedImages:
    """ImageReader over Graph: a channel image's hosted copy, read with the Graph token."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    async def download(self, url: str, dest: Path, *, max_bytes: int) -> RemoteFile:
        if not url.startswith(GRAPH_ROOT + "/"):
            raise InvalidRequest("Only Graph hosted-content URLs can be downloaded through Graph.")
        content_type, size = await self._graph.download(url, dest, max_bytes=max_bytes)
        return RemoteFile(name=None, content_type=content_type, size=size)
