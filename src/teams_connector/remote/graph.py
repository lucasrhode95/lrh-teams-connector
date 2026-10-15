"""Generic Microsoft Graph plumbing: URLs, paging, JSON batching, downloads.

Every request asks Graph to include evolvable enum members, so system events and shared channels are
reported as such instead of ``unknownFutureValue``. Continuation links are opaque and must stay on the
Graph host.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from teams_connector.config import GRAPH_ROOT
from teams_connector.domain.errors import InvalidRequest, Upstream
from teams_connector.remote.transport import (
    Credential,
    Transport,
    describe_failure,
    request_id,
    service_error,
)

PREFER = {"Prefer": "include-unknown-enum-members"}
BATCH_LIMIT = 20  # Graph JSON batching maximum per request
BATCH_CONCURRENCY = 2
BATCH_RETRIES = 4  # rounds of re-sending throttled sub-requests
BATCH_RETRY_STATUSES = frozenset({429, 502, 503, 504})
DEFAULT_RETRY_WAIT = 5.0
MAX_RETRY_WAIT = 30.0
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024


def relative(path: str, params: Mapping[str, Any] | None = None) -> str:
    """A version-relative URL such as ``/users/{id}?$select=id`` (used directly in $batch)."""
    query = urlencode({k: v for k, v in (params or {}).items() if v is not None}, quote_via=quote)
    return f"{path}?{query}" if query else path


def segment(value: str) -> str:
    """One URL path segment (Teams thread ids contain ':' and '@')."""
    return quote(value, safe="")


@dataclass
class SubResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Graph:
    def __init__(self, transport: Transport, credential: Credential) -> None:
        self._transport = transport
        self._credential = credential
        self._batch_gate = asyncio.Semaphore(BATCH_CONCURRENCY)

    def _absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith("https://"):
            if not path_or_url.startswith(GRAPH_ROOT + "/"):
                raise InvalidRequest("Continuation link does not point at Microsoft Graph.")
            return path_or_url
        return GRAPH_ROOT + path_or_url

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = self._absolute(relative(path, params) if not path.startswith("https://") else path)
        data = await self._transport.json("GET", url, credential=self._credential, headers=PREFER)
        return data if isinstance(data, dict) else {}

    async def post(
        self, path: str, body: Any, *, idempotent: bool, prefer: str | None = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """POST JSON. Read-style POSTs ($batch) are idempotent and retried; writes are sent once. ``prefer``
        replaces the default ``Prefer`` header."""
        response = await self._transport.request(
            "POST",
            self._absolute(path),
            credential=self._credential,
            headers={"Prefer": prefer} if prefer else PREFER,
            json_body=body,
            retry=idempotent,
        )
        return _json_object(response.content), dict(response.headers)

    async def put_content(self, path: str, data: bytes) -> dict[str, Any]:
        """PUT raw bytes (a file's content). A write: sent once, never retried."""
        response = await self._transport.request(
            "PUT",
            self._absolute(path),
            credential=self._credential,
            headers={"Content-Type": "application/octet-stream"},
            content=data,
            retry=False,
        )
        return _json_object(response.content)

    async def upload_session(
        self,
        method: str,
        upload_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """A request to an upload session's pre-authenticated ``uploadUrl``, sent without the Graph token.
        Returns the status and the JSON body (empty when there is none)."""
        response = await self._transport.upload_session(method, upload_url, headers=headers, content=content)
        return response.status_code, _json_object(response.content)

    async def page(
        self, path_or_link: str, params: Mapping[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of a collection and the opaque link to the next page (if any)."""
        data = await self.get(path_or_link, None if path_or_link.startswith("https://") else params)
        items = [x for x in data.get("value", []) if isinstance(x, dict)]
        link = data.get("@odata.nextLink")
        return items, link if isinstance(link, str) else None

    async def collect(
        self, path: str, params: Mapping[str, Any] | None = None, *, max_items: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Follow pages up to ``max_items``. Returns (items, truncated)."""
        items, link = await self.page(path, params)
        seen_links: set[str] = set()
        while link and len(items) < max_items:
            if link in seen_links:
                raise Upstream("Graph collection repeated a continuation link; pagination stopped.")
            seen_links.add(link)
            more, next_link = await self.page(link)
            items.extend(more)
            if next_link and len(items) < max_items and next_link in seen_links:
                raise Upstream("Graph collection repeated a continuation link; pagination stopped.")
            link = next_link
        return items[:max_items], bool(link) or len(items) > max_items

    async def batch(self, requests: Mapping[str, str]) -> dict[str, SubResponse]:
        """GET many relative URLs via $batch. Returns {caller key: sub-response}, one per request.

        Batch request ids are numbers assigned here, never the caller's keys. At most BATCH_LIMIT requests
        per batch and BATCH_CONCURRENCY batches in flight. Transient sub-requests are re-sent in new
        batches after the advised delay, up to BATCH_RETRIES rounds; final sub-responses are returned.
        """
        results: dict[str, SubResponse] = {}
        pending = list(requests)

        async def send(keys: list[str]) -> dict[str, SubResponse]:
            async with self._batch_gate:
                return await self._batch_once({k: requests[k] for k in keys})

        for attempt in range(BATCH_RETRIES + 1):
            chunks = [pending[i : i + BATCH_LIMIT] for i in range(0, len(pending), BATCH_LIMIT)]
            for part in await asyncio.gather(*(send(c) for c in chunks)):
                results.update(part)
            pending = [k for k in pending if results[k].status in BATCH_RETRY_STATUSES]
            if not pending or attempt == BATCH_RETRIES:
                break
            await self._transport.sleep(max(_retry_after(results[k].headers) for k in pending))
        return results

    async def _batch_once(self, requests: Mapping[str, str]) -> dict[str, SubResponse]:
        keys = list(requests)
        body = {
            "requests": [
                {"id": str(index), "method": "GET", "url": requests[key], "headers": dict(PREFER)}
                for index, key in enumerate(keys)
            ]
        }
        data, _ = await self.post("/$batch", body, idempotent=True)
        out: dict[str, SubResponse] = {}
        for response in data.get("responses", []):
            try:
                key = keys[int(response.get("id"))]
            except (TypeError, ValueError, IndexError):
                raise Upstream("Graph batch response has an unknown request id.") from None
            body_part = response.get("body")
            headers = response.get("headers")
            out[key] = SubResponse(
                int(response.get("status", 0)),
                body_part if isinstance(body_part, dict) else {},
                {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
            )
        missing = set(keys) - set(out)
        if missing:
            raise Upstream(f"Graph batch response is missing {len(missing)} sub-responses.")
        return out

    async def get_bytes(self, path: str, *, max_bytes: int) -> tuple[bytes, str | None]:
        return await self._transport.get_bytes(
            self._absolute(path), credential=self._credential, headers=PREFER, max_bytes=max_bytes
        )

    async def download(
        self, path: str, dest: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES
    ) -> tuple[str | None, int]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            return await self._transport.download(
                self._absolute(path), handle, credential=self._credential, headers=PREFER, max_bytes=max_bytes
            )


def _json_object(content: bytes) -> dict[str, Any]:
    """A write's JSON body as an object; an empty or non-object body is an empty object."""
    try:
        data = json.loads(content) if content.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _retry_after(headers: Mapping[str, str]) -> float:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
    try:
        return min(max(float(value), 1.0), MAX_RETRY_WAIT)
    except ValueError:
        return DEFAULT_RETRY_WAIT


def sub_failure(response: SubResponse, *, detail: str | None = None) -> Exception:
    """The domain error for a failed $batch sub-response."""
    code, message = service_error(response.body)
    return describe_failure(
        status=response.status,
        code=code,
        message=message,
        request=request_id(response.headers),
        host="graph.microsoft.com",
        detail=detail,
    )
