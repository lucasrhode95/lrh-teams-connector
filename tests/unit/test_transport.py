from __future__ import annotations

from io import BytesIO

import httpx
import pytest

from teams_connector.domain.errors import InvalidRequest, PermissionDenied, Upstream, WriteOutcomeUnknown
from teams_connector.remote.transport import Transport, describe_failure


class Credential:
    def __init__(self) -> None:
        self.renewals: list[dict[str, object] | None] = []

    async def headers(self, renewal: dict[str, object] | None) -> dict[str, str]:
        self.renewals.append(renewal)
        return {"Authorization": "Bearer synthetic-token"}

    def sign_in_command(self) -> str:
        return "teams-connector auth"


async def test_get_retries_transient_failures_and_honors_retry_after() -> None:
    attempts = 0
    delays: list[float] = []

    async def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"value": ["synthetic"]})

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client, sleep=sleep)
    try:
        response = await transport.json("GET", "https://graph.microsoft.com/v1.0/me", credential=Credential())
    finally:
        await transport.aclose()

    assert response == {"value": ["synthetic"]}
    assert attempts == 2
    assert delays == [7]


async def test_writes_retry_only_failures_that_prove_nothing_was_applied() -> None:
    attempts: list[str] = []
    delays: list[float] = []
    statuses: list[int] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(request.method)
        if request.headers.get("x-mode") == "connect" and len(attempts) == 1:
            raise httpx.ConnectError("refused", request=request)  # never sent
        if request.headers.get("x-mode") == "lost":
            raise httpx.ReadError("reset", request=request)  # sent, no answer
        return httpx.Response(statuses.pop(0), json={"error": "synthetic"})

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client, sleep=sleep)
    url = "https://graph.microsoft.com/v1.0/me/messages"

    async def write(mode: str = "status") -> httpx.Response:
        return await transport.request("POST", url, credential=Credential(), headers={"x-mode": mode})

    try:
        statuses[:] = [429, 503, 201]  # throttled, unavailable: nothing was processed, so retried
        assert (await write()).status_code == 201 and len(attempts) == 3

        attempts.clear()
        statuses[:] = [500]  # a server error may have applied the write: sent once
        with pytest.raises(Upstream):
            await write()
        assert len(attempts) == 1

        attempts.clear()
        statuses[:] = [504]
        with pytest.raises(WriteOutcomeUnknown, match="may or may not"):
            await write()
        assert len(attempts) == 1

        attempts.clear()
        with pytest.raises(WriteOutcomeUnknown, match="may or may not"):
            await write("lost")
        assert len(attempts) == 1

        attempts.clear()
        statuses[:] = [201]
        assert (await write("connect")).status_code == 201 and len(attempts) == 2
    finally:
        await transport.aclose()


async def test_401_renews_the_credential_once() -> None:
    attempts = 0
    credential = Credential()

    async def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401) if attempts == 1 else httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client)
    try:
        assert await transport.json("GET", "https://graph.microsoft.com/v1.0/me", credential=credential) == {
            "ok": True
        }
    finally:
        await transport.aclose()

    assert attempts == 2
    assert credential.renewals == [None, {"force_refresh": True}]


async def test_download_redirect_does_not_forward_the_bearer_token() -> None:
    headers_by_host: dict[str, str | None] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        headers_by_host[host] = request.headers.get("authorization")
        if host == "graph.microsoft.com":
            return httpx.Response(
                302,
                headers={"Location": "https://tenant.sharepoint.com/download?token=synthetic"},
            )
        return httpx.Response(200, content=b"synthetic file", headers={"Content-Type": "text/plain"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client)
    destination = BytesIO()
    try:
        content_type, size = await transport.download(
            "https://graph.microsoft.com/v1.0/shares/id/driveItem/content",
            destination,
            credential=Credential(),
            max_bytes=100,
        )
    finally:
        await transport.aclose()

    assert content_type == "text/plain" and size == len(b"synthetic file")
    assert destination.getvalue() == b"synthetic file"
    assert headers_by_host == {
        "graph.microsoft.com": "Bearer synthetic-token",
        "tenant.sharepoint.com": None,
    }


async def test_a_download_follows_one_redirect_to_a_named_media_host_without_credentials() -> None:
    headers_by_host: dict[str, str | None] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        headers_by_host[host] = request.headers.get("authorization")
        if host == "graph.microsoft.com":
            return httpx.Response(
                302, headers={"Location": "https://media.example.invalid/page?sig=synthetic"}
            )
        return httpx.Response(200, content=b"synthetic page", headers={"Content-Type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client)
    destination = BytesIO()
    try:
        content_type, size = await transport.download(
            "https://graph.microsoft.com/v1.0/shares/id/driveItem/content",
            destination,
            credential=Credential(),
            max_bytes=100,
            redirect_hosts=frozenset({"media.example.invalid"}),
        )
    finally:
        await transport.aclose()

    assert content_type == "text/html" and size == len(b"synthetic page")
    assert destination.getvalue() == b"synthetic page"
    assert headers_by_host == {"graph.microsoft.com": "Bearer synthetic-token", "media.example.invalid": None}


async def test_a_redirect_to_a_host_not_named_is_still_refused() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(302, headers={"Location": "https://other.example.invalid/page"})
        return httpx.Response(200, content=b"never read")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client)
    try:
        with pytest.raises(Upstream, match="non-allowlisted"):
            await transport.download(
                "https://graph.microsoft.com/v1.0/shares/id/driveItem/content",
                BytesIO(),
                credential=Credential(),
                max_bytes=100,
                redirect_hosts=frozenset({"media.example.invalid"}),
            )
    finally:
        await transport.aclose()


async def test_non_allowlisted_hosts_and_oversized_downloads_are_rejected() -> None:
    requests = 0

    async def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=b"too large")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client)
    try:
        with pytest.raises(InvalidRequest, match="non-allowlisted"):
            await transport.request(
                "GET", "https://graph.microsoft.com.evil.invalid/me", credential=Credential()
            )
        with pytest.raises(InvalidRequest, match="exceeds"):
            await transport.download(
                "https://graph.microsoft.com/v1.0/shares/id/driveItem/content",
                BytesIO(),
                credential=Credential(),
                max_bytes=4,
            )
    finally:
        await transport.aclose()

    assert requests == 1


async def test_upload_sessions_reach_only_sharepoint_and_carry_no_credential() -> None:
    seen: list[tuple[str, str | None]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("authorization")))
        if request.method == "PUT":
            return httpx.Response(500, json={"error": {"code": "generalException"}})
        return httpx.Response(200, json={"nextExpectedRanges": ["0-"]})

    upload_url = "https://tenant-my.sharepoint.com/personal/user/_api/v2.0/drive/uploadSession/1"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client)
    try:
        status = await transport.upload_session("GET", upload_url)
        with pytest.raises(Upstream):
            await transport.upload_session("PUT", upload_url, content=b"chunk")
        for refused in (
            "https://graph.microsoft.com/v1.0/me",
            "http://tenant-my.sharepoint.com/upload",
            "https://sharepoint.com.evil.invalid/upload",
        ):
            with pytest.raises(Upstream, match="non-allowlisted"):
                await transport.upload_session("PUT", refused, content=b"chunk")
    finally:
        await transport.aclose()

    assert status.status_code == 200
    assert seen == [("GET", None), ("PUT", None)]  # the failed chunk was sent once


def test_a_roster_denial_says_the_account_is_not_a_member_without_echoing_identifiers() -> None:
    details = (
        '{"subCode":"AclCheckFailed","details":"The initiator 8:orgid:00000000-0000-0000-0000-000000000001 '
        "is not a member of the roster 00000000-0000-0000-0000-000000000002 in the generic thread "
        '19:meeting_synthetic@thread.v2"}'
    )

    error = describe_failure(status=403, code="209", message=details, request="synthetic-request")

    assert isinstance(error, PermissionDenied) and isinstance(error, Upstream)
    assert "not a member of this conversation" in str(error)
    assert "AclCheckFailed" in str(error) and "synthetic-request" in str(error)
    assert "8:orgid" not in str(error) and "19:meeting" not in str(error)
