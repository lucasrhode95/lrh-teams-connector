from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from teams_connector.domain.errors import Upstream
from teams_connector.remote.graph import BATCH_RETRIES, Graph
from teams_connector.remote.transport import Credential, Transport

GRAPH = "https://graph.microsoft.com/v1.0"


class PagingTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = iter(responses)
        self.urls: list[str] = []

    async def json(self, method: str, url: str, **_: Any) -> dict[str, Any]:
        assert method == "GET"
        self.urls.append(url)
        return next(self.responses)


class BatchTransport:
    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self.batches = iter(batches)
        self.requests: list[dict[str, Any]] = []
        self.delays: list[float] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert method == "POST" and url == f"{GRAPH}/$batch"
        assert kwargs["retry"] is True
        self.requests.append(kwargs["json_body"])
        return httpx.Response(200, json={"responses": next(self.batches)})

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)


class NoCredential:
    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        return {}

    def sign_in_command(self) -> str:
        return "teams-connector auth"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "links",
    [
        [f"{GRAPH}/items?skip=1", f"{GRAPH}/items?skip=1"],
        [f"{GRAPH}/items?skip=1", f"{GRAPH}/items?skip=2", f"{GRAPH}/items?skip=1"],
    ],
)
async def test_collect_rejects_repeated_continuation_links(links: list[str]) -> None:
    pages = PagingTransport(
        [
            {"value": [], "@odata.nextLink": links[0]},
            *({"value": [], "@odata.nextLink": link} for link in links[1:]),
        ]
    )
    graph = Graph(cast(Transport, pages), cast(Credential, NoCredential()))

    with pytest.raises(Upstream, match="repeated a continuation link"):
        await graph.collect("/items", max_items=10)

    assert len(pages.urls) == len(links)


@pytest.mark.asyncio
async def test_collect_keeps_opaque_paging_and_item_bound() -> None:
    pages = PagingTransport(
        [
            {"value": [{"id": "first"}], "@odata.nextLink": f"{GRAPH}/items?skip=1"},
            {"value": [{"id": "second"}], "@odata.nextLink": f"{GRAPH}/items?skip=2"},
        ]
    )
    graph = Graph(cast(Transport, pages), cast(Credential, NoCredential()))

    items, truncated = await graph.collect("/items", max_items=2)

    assert [item["id"] for item in items] == ["first", "second"]
    assert truncated
    assert len(pages.urls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_batch_retries_only_transient_get_subresponses(status: int) -> None:
    transport = BatchTransport(
        [
            [
                {"id": "0", "status": status, "headers": {"Retry-After": "2"}},
                {"id": "1", "status": 200, "body": {"id": "stable"}},
            ],
            [{"id": "0", "status": 200, "body": {"id": "retried"}}],
        ]
    )
    graph = Graph(cast(Transport, transport), cast(Credential, NoCredential()))

    result = await graph.batch({"retry": "/me/messages/1", "stable": "/me/messages/2"})

    assert result["retry"].status == 200
    assert result["stable"].body == {"id": "stable"}
    assert [len(batch["requests"]) for batch in transport.requests] == [2, 1]
    assert transport.requests[1]["requests"][0]["url"] == "/me/messages/1"
    assert transport.delays == [2.0]


@pytest.mark.asyncio
async def test_batch_returns_final_transient_failure_after_bounded_retries() -> None:
    transport = BatchTransport([[{"id": "0", "status": 503}] for _ in range(BATCH_RETRIES + 1)])
    graph = Graph(cast(Transport, transport), cast(Credential, NoCredential()))

    result = await graph.batch({"retry": "/me/messages/1"})

    assert result["retry"].status == 503
    assert len(transport.requests) == BATCH_RETRIES + 1
    assert len(transport.delays) == BATCH_RETRIES
