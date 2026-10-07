"""The one HTTP client for Microsoft calls: credentials, host allowlist, retries, error mapping.

- One ``httpx.AsyncClient`` per process (keep-alive within a call).
- No redirects, and only allowlisted hosts. A download may follow one redirect to a pre-authenticated
  SharePoint URL, without credentials.
- Credentials are pluggable: a bearer token profile (Graph, CSA) or the Teams Skype token
  (Chat Service). A rejected credential (401) is renewed once.
- Retries only when the caller marks the request idempotent (GETs by default), honoring
  429 / Retry-After. Writes are never retried.
- Concurrency limiter: at most 4 requests in flight per process.
- Logs metadata only: no URLs with query text, no bodies, no tokens.
- Errors name the operation in progress (``operation()``), the service error code and message
  (shortened), and the request id, so a failure can be traced without exposing message content.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from typing import Any, BinaryIO, Protocol
from urllib.parse import urlsplit

import httpx

from teams_connector.domain.errors import (
    AuthenticationRequired,
    InvalidRequest,
    NotFound,
    Throttled,
    Upstream,
    WriteOutcomeUnknown,
)

log = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset({"graph.microsoft.com"})
ALLOWED_HOST_SUFFIXES = ("teams.microsoft.com", "teams.cloud.microsoft")
DOWNLOAD_HOST_SUFFIXES = (".sharepoint.com",)  # pre-authenticated download URLs only
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_CONCURRENCY = 4
MAX_ERROR_TEXT = 200

_operation: ContextVar[str | None] = ContextVar("operation", default=None)


@contextlib.contextmanager
def operation(name: str) -> Iterator[None]:
    """Name what the code below is doing; errors raised inside it say so ("reading chat history")."""
    token = _operation.set(name)
    try:
        yield
    finally:
        _operation.reset(token)


class _Token(Protocol):
    @property
    def value(self) -> str: ...


class TokenSource(Protocol):
    def get_token(
        self, profile: str, *, force_refresh: bool = False, claims_challenge: str | None = None
    ) -> _Token: ...

    def sign_in_command(self, profile: str = ...) -> str: ...


class Credential(Protocol):
    """Request authentication. ``renewal`` (after a 401) asks for a fresh credential."""

    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]: ...

    def sign_in_command(self) -> str: ...


class Bearer:
    """A bearer token from one token profile."""

    def __init__(self, tokens: TokenSource, profile: str) -> None:
        self._tokens = tokens
        self.profile = profile

    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.get_token(self.profile, **(renewal or {})).value}"}

    def sign_in_command(self) -> str:
        return self._tokens.sign_in_command(self.profile)


def host_allowed(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower()
    return host in ALLOWED_HOSTS or any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES
    )


class Transport:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        max_concurrency: int = MAX_CONCURRENCY,
        max_attempts: int = 4,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False
        )
        self._limit = asyncio.Semaphore(max_concurrency)
        self._max_attempts = max_attempts
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def sleep(self, seconds: float) -> None:
        """Wait before a retry (injectable for tests)."""
        await self._sleep(seconds)

    async def request(
        self,
        method: str,
        url: str,
        *,
        credential: Credential,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        retry: bool | None = None,
    ) -> httpx.Response:
        """Send a request and return a successful response, or raise a domain error."""
        _check_host(url)
        retry = (method.upper() == "GET") if retry is None else retry
        attempts = self._max_attempts if retry else 1
        renewal: dict[str, Any] | None = None  # after a 401: how to renew the credential, for one request
        renewed = False
        attempt = 0
        while attempt < attempts:
            attempt += 1
            request_headers = {**(await credential.headers(renewal)), **(headers or {})}
            renewal = None
            started = time.monotonic()
            try:
                async with self._limit:
                    response = await self._client.request(
                        method, url, headers=request_headers, json=json_body, content=content
                    )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt < attempts:
                    await self._sleep(min(2**attempt, 20))
                    continue
                raise Upstream(f"Could not reach {urlsplit(url).hostname} ({type(exc).__name__}).") from None
            except httpx.HTTPError as exc:  # sent, but no complete response
                if attempt < attempts:
                    await self._sleep(min(2**attempt, 20))
                    continue
                if not retry:  # a write may have been applied: the caller must check, never resend
                    raise WriteOutcomeUnknown(
                        f"No response from {urlsplit(url).hostname} after sending ({type(exc).__name__}); "
                        "the change may or may not have been applied."
                    ) from None
                raise Upstream(
                    f"No complete response from {urlsplit(url).hostname} ({type(exc).__name__})."
                ) from None
            log.debug(
                "http %s %s -> %s in %dms",
                method,
                _path(url),
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            if response.status_code == 401:
                if not renewed:  # the credential was rejected (revoked, expired early): renew once
                    renewed, renewal = True, _renewal(response)
                    attempt -= 1
                    continue
                raise _sign_in_required(credential, response)
            if response.status_code in RETRY_STATUSES and attempt < attempts:
                await self._sleep(_retry_after(response, attempt))
                continue
            if response.is_success:
                return response
            if not retry and response.status_code == 504:  # the gateway gave up; the write may have landed
                raise WriteOutcomeUnknown(
                    "Microsoft did not confirm the request in time (HTTP 504); "
                    "it may or may not have been applied."
                )
            raise _error_for(response)
        raise AssertionError("unreachable")

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self.request(method, url, **kwargs)
        if len(response.content) > MAX_JSON_BYTES:
            raise Upstream("Response too large.")
        if not response.content.strip():
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise Upstream("Expected a JSON response.") from None

    async def get_bytes(
        self, url: str, *, credential: Credential, headers: Mapping[str, str] | None = None, max_bytes: int
    ) -> tuple[bytes, str | None]:
        """A small binary GET (photos). Returns (bytes, content type)."""
        response = await self.request("GET", url, credential=credential, headers=headers)
        if len(response.content) > max_bytes:
            raise Upstream("Response too large.")
        return response.content, response.headers.get("content-type")

    async def download(
        self,
        url: str,
        dest: BinaryIO,
        *,
        credential: Credential,
        headers: Mapping[str, str] | None = None,
        max_bytes: int,
    ) -> tuple[str | None, int]:
        """Stream a GET response body into ``dest``. Returns (content type, size).

        A redirect is followed once, without credentials, and only to a pre-authenticated
        SharePoint download host.
        """
        _check_host(url)
        renewal: dict[str, Any] | None = None
        renewed = False
        while True:
            request_headers = {**(await credential.headers(renewal)), **(headers or {})}
            renewal = None
            async with self._limit, self._client.stream("GET", url, headers=request_headers) as response:
                if response.status_code == 401:
                    await response.aread()
                    if not renewed:
                        renewed, renewal = True, _renewal(response)
                        continue
                    raise _sign_in_required(credential, response)
                if response.status_code in (301, 302, 303, 307, 308):
                    target = response.headers.get("location", "")
                    await response.aread()
                    return await self._download_redirect(target, dest, max_bytes=max_bytes)
                if not response.is_success:
                    await response.aread()
                    raise _error_for(response)
                return await _stream_into(response, dest, max_bytes)

    async def _download_redirect(self, url: str, dest: BinaryIO, *, max_bytes: int) -> tuple[str | None, int]:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host.endswith(DOWNLOAD_HOST_SUFFIXES):
            raise Upstream(f"Refusing a download redirect to a non-allowlisted host: {host or '(none)'}")
        async with self._limit, self._client.stream("GET", url) as response:
            if not response.is_success:
                await response.aread()
                raise _error_for(response)
            return await _stream_into(response, dest, max_bytes)


async def _stream_into(response: httpx.Response, dest: BinaryIO, max_bytes: int) -> tuple[str | None, int]:
    dest.seek(0)
    dest.truncate()
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise InvalidRequest(f"Download exceeds the {max_bytes // (1024 * 1024)} MiB limit.")
        dest.write(chunk)
    return response.headers.get("content-type"), size


def _check_host(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not host_allowed(parts.hostname):
        raise InvalidRequest(f"Refusing to call a non-allowlisted URL host: {parts.hostname}")


def _path(url: str) -> str:
    """The URL path for logs, with ids (long or colon-bearing segments) replaced: no ids in clear."""
    return "/".join(
        "{id}" if len(part) > 24 or ":" in part or "@" in part else part
        for part in urlsplit(url).path.split("/")
    )


def _sign_in_required(credential: Credential, response: httpx.Response) -> AuthenticationRequired:
    code = error_code(response) or "no error code"
    return AuthenticationRequired(
        f"Microsoft rejected the sign-in for this request (HTTP 401, {code}), also after renewing it.",
        command=credential.sign_in_command(),
    )


_CLAIMS = re.compile(r'claims="([^"]+)"')


def _renewal(response: httpx.Response) -> dict[str, Any]:
    """How to renew a rejected token: force a refresh, with the CAE claims challenge if any."""
    challenge = _CLAIMS.search(response.headers.get("www-authenticate", ""))
    if challenge:
        try:
            claims = base64.b64decode(challenge.group(1) + "=" * (-len(challenge.group(1)) % 4)).decode()
            return {"claims_challenge": claims}
        except (ValueError, UnicodeDecodeError):
            pass
    return {"force_refresh": True}


def _retry_after(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("retry-after", "")
    try:
        return min(float(value), 30.0)
    except ValueError:
        return float(min(2**attempt, 20))


def service_error(body: Any) -> tuple[str | None, str | None]:
    """(code, message) of a Microsoft error body ``{"error": {"code", "message"}}`` (or a flat variant)."""
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if isinstance(error, dict):
        code = error.get("code") if isinstance(error.get("code"), str) else None
        message = error.get("message") if isinstance(error.get("message"), str) else None
        return code, message
    code = body.get("errorCode") or body.get("code")
    message = body.get("message")
    return (str(code) if code is not None else None), (message if isinstance(message, str) else None)


def error_code(response: httpx.Response) -> str | None:
    try:
        return service_error(response.json())[0]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def request_id(headers: Mapping[str, str]) -> str | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    value = lowered.get("request-id") or lowered.get("client-request-id") or lowered.get("ms-cv")
    return value[:64] if value else None


def describe_failure(
    *,
    status: int,
    code: str | None,
    message: str | None,
    request: str | None,
    host: str | None = None,
    detail: str | None = None,
) -> Exception:
    """The domain error for a failed Microsoft response, with sanitized diagnostics.

    The service message is shortened and flattened to one line. Neither Graph nor the Teams services
    echo message bodies in error messages; tokens never appear in them.
    """
    where = "Microsoft Graph" if host == "graph.microsoft.com" else "Microsoft Teams"
    parts = [f"HTTP {status}", code or "no error code"]
    text = ", ".join(parts)
    if message:
        text += ": " + re.sub(r"\s+", " ", message).strip()[:MAX_ERROR_TEXT]
    if request:
        text += f"; request-id {request}"
    prefix = f"While {_operation.get()}: " if _operation.get() else ""
    suffix = f" {detail}" if detail else ""
    if status == 404:
        return NotFound(f"{prefix}Not found ({text}).{suffix}")
    if status == 429:
        return Throttled(
            f"{prefix}{where} is throttling requests ({text}).{suffix} "
            "Wait and retry, and avoid parallel calls."
        )
    if status == 400:
        return InvalidRequest(f"{prefix}{where} rejected the request ({text}).{suffix}")
    if status == 403:  # authorization denied for this item or action; the sign-in itself is fine
        return Upstream(f"{prefix}{where} denied access ({text}).{suffix}")
    return Upstream(f"{prefix}{where} returned an error ({text}).{suffix}")


def _error_for(response: httpx.Response) -> Exception:
    host = urlsplit(str(response.request.url)).hostname if response.request else None
    message = None
    with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
        message = service_error(response.json())[1]
    return describe_failure(
        status=response.status_code,
        code=error_code(response),
        message=message,
        request=request_id(response.headers),
        host=host,
    )
