"""Local web UI: a Starlette JSON API over the shared service, plus the static page.

Security: bound to 127.0.0.1 by the launcher; every /api request must carry the per-run session
token and a localhost Host header (blocks other local pages and DNS rebinding). Message content is
untrusted; the page renders it as text only, and loads images only from this server.

Exports run as background tasks (one at a time) that report progress and can be cancelled; the
finished file is downloaded from the job.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as day_time
from importlib import resources
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from teams_connector.bootstrap import AppContext
from teams_connector.domain.errors import (
    AuthenticationRequired,
    ConnectorError,
    InvalidRequest,
    NotFound,
    Throttled,
)
from teams_connector.domain.models import ExportArtifact, ExportProgress, ExportRequest, PreviewOutput

STATIC = resources.files("teams_connector.surfaces.web") / "static"
_STATUS = {AuthenticationRequired: 401, InvalidRequest: 400, NotFound: 404, Throttled: 429}
_USER_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
CSP = (
    "default-src 'none'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
MAX_MESSAGE_LIMIT = 1_000_000


class Activity:
    def __init__(self) -> None:
        self.last = time.monotonic()

    def touch(self) -> None:
        self.last = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last


@dataclass
class ExportJob:
    id: str
    status: str = "running"  # running, complete, cancelled, failed
    progress: ExportProgress = field(default_factory=lambda: ExportProgress(phase="retrieving"))
    artifact: ExportArtifact | None = None
    error: dict[str, str] | None = None
    task: asyncio.Task[None] | None = None


@dataclass
class PreviewJob:
    id: str
    status: str = "running"  # running, complete, cancelled, failed
    progress: ExportProgress = field(default_factory=lambda: ExportProgress(phase="retrieving"))
    result: PreviewOutput | None = None
    error: dict[str, str] | None = None
    task: asyncio.Task[None] | None = None


def _guard(token: str, port: int, activity: Activity) -> type[BaseHTTPMiddleware]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Guard(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            if request.headers.get("host") not in allowed_hosts:
                return JSONResponse({"error": "Forbidden host."}, status_code=403)
            if request.url.path.startswith("/api/") and request.headers.get("x-session-token") != token:
                return JSONResponse({"error": "Missing or wrong session token."}, status_code=403)
            activity.touch()
            response = await call_next(request)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            return response

    return Guard


def _flag(request: Request, name: str, default: bool = False) -> bool:
    value = request.query_params.get(name)
    return default if value is None else value.lower() in ("1", "true", "yes")


def _error(exc: Exception) -> JSONResponse:
    status = next((code for kind, code in _STATUS.items() if isinstance(exc, kind)), 502)
    return JSONResponse({"error": str(exc), "kind": type(exc).__name__}, status_code=status)


def local_day(value: Any, *, end: bool, offset_minutes: Any, name: str) -> datetime | None:
    """A calendar date picked in the browser, as the start or end of that day in the browser's zone."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise InvalidRequest(f"{name} must be a calendar date (YYYY-MM-DD).")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise InvalidRequest(f"{name} must use the YYYY-MM-DD format.") from None
    if (
        isinstance(offset_minutes, bool)
        or not isinstance(offset_minutes, int)
        or not -840 <= offset_minutes <= 840
    ):
        raise InvalidRequest("The browser's UTC offset must be whole minutes from -840 to 840.")
    zone = timezone(timedelta(minutes=-offset_minutes))
    return datetime.combine(day, day_time.max if end else day_time.min, tzinfo=zone)


def export_request(payload: Any) -> ExportRequest:
    if not isinstance(payload, dict):
        raise InvalidRequest("The request body must be a JSON object.")
    ids = payload.get("conversation_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i.strip() for i in ids):
        raise InvalidRequest("Select one or more conversations.")
    since = local_day(
        payload.get("from_date"),
        end=False,
        offset_minutes=payload.get("from_utc_offset_minutes"),
        name="from_date",
    )
    until = local_day(
        payload.get("to_date"), end=True, offset_minutes=payload.get("to_utc_offset_minutes"), name="to_date"
    )
    if since and until and since > until:
        raise InvalidRequest("from_date must be earlier than or equal to to_date.")
    limit = payload.get("message_limit")
    if limit in (None, ""):
        limit = None
    elif isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MESSAGE_LIMIT:
        raise InvalidRequest(f"message_limit must be between 1 and {MAX_MESSAGE_LIMIT:,}, or blank.")
    include_attachments = payload.get("include_attachments", True)
    if not isinstance(include_attachments, bool):
        raise InvalidRequest("include_attachments must be true or false.")
    include_transcripts = payload.get("include_transcripts", True)
    if not isinstance(include_transcripts, bool):
        raise InvalidRequest("include_transcripts must be true or false.")
    include_summaries = payload.get("include_summaries", True)
    if not isinstance(include_summaries, bool):
        raise InvalidRequest("include_summaries must be true or false.")
    return ExportRequest(
        conversation_ids=ids,
        since=since,
        until=until,
        message_limit=limit,
        include_attachments=include_attachments,
        include_transcripts=include_transcripts,
        include_summaries=include_summaries,
    )


def preview_request(payload: Any) -> tuple[ExportRequest, bool]:
    """Entry point: validate one preview conversation, export settings, and the full-range flag."""
    if not isinstance(payload, dict):
        raise InvalidRequest("The request body must be a JSON object.")
    entire_range = payload.get("entire_range", False)
    if not isinstance(entire_range, bool):
        raise InvalidRequest("entire_range must be true or false.")
    request = export_request(payload)
    if len(request.conversation_ids) != 1:
        raise InvalidRequest("Preview exactly one conversation.")
    return request, entire_range


def create_app(context: AppContext, *, session_token: str, port: int, activity: Activity) -> Starlette:
    index_html = (
        (STATIC / "index.html").read_text(encoding="utf-8").replace("{{SESSION_TOKEN}}", session_token)
    )
    jobs: dict[str, ExportJob] = {}
    preview_job: PreviewJob | None = None

    def api(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except ConnectorError as exc:
                return _error(exc)
            except ValidationError as exc:
                return JSONResponse({"error": str(exc), "kind": "InvalidRequest"}, status_code=400)

        return wrapped

    async def index(_: Request) -> Response:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})

    async def status(_: Request) -> Response:
        cache = context.tokens.status()
        return JSONResponse(
            {
                "account": next((a.username for a in cache.accounts), None),
                "client_credential_cached": {p.profile: p.client_credential_cached for p in cache.profiles},
                "sign_in_command": context.tokens.sign_in_command(),
            }
        )

    async def chats(request: Request) -> Response:
        services = await context.services()
        include_empty = _flag(request, "include_empty")
        if _flag(request, "cached"):  # the stored catalog, without contacting Teams
            items = [c for c in services.store.chats() if include_empty or c.is_empty is not True]
            source = "local"
        else:
            source = await services.catalog.ensure(refresh=_flag(request, "refresh"))
            items = [c for c in services.store.chats() if include_empty or c.is_empty is not True]
        return JSONResponse(
            {
                "chats": [c.model_dump(mode="json") for c in items],
                "account": {
                    "name": services.me.name,
                    "email": services.account.username,
                    "photo_user_id": services.account.object_id,
                },
                "coverage": services.catalog.coverage(source, len(items)).model_dump(mode="json"),
            }
        )

    async def photo(request: Request) -> Response:
        user_id = request.path_params["user_id"]
        if not _USER_ID.fullmatch(user_id):
            raise NotFound("Unknown person.")
        found = await (await context.services()).photos.photo(user_id)
        if found is None:
            return Response(status_code=404, headers={"Cache-Control": "private, max-age=3600"})
        content, content_type = found
        etag = f'"{hashlib.sha256(content).hexdigest()[:32]}"'
        headers = {"Cache-Control": "private, max-age=3600", "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content, media_type=content_type, headers=headers)

    async def start_export(request: Request) -> Response:
        export = export_request(await request.json())
        if any(job.status == "running" for job in jobs.values()):
            return JSONResponse({"error": "Another export is running. Wait for it or cancel it first."},
                                status_code=409)  # fmt: skip
        services = await context.services()
        for chat_id in export.conversation_ids:  # fail fast on unknown chats, before the job starts
            await services.catalog.get(chat_id)
        await cancel_active_preview()
        job = ExportJob(id=uuid.uuid4().hex)

        def report(progress: ExportProgress) -> None:
            job.progress = progress

        async def run() -> None:
            try:
                job.artifact = await services.exports.export(export, progress=report)
                job.status = "complete"
            except asyncio.CancelledError:
                job.status = "cancelled"
            except ConnectorError as exc:
                job.status = "failed"
                job.error = {"message": str(exc), "kind": type(exc).__name__}

        job.task = asyncio.create_task(run())
        jobs[job.id] = job
        return JSONResponse({"id": job.id, "status": job.status}, status_code=202)

    async def cancel_active_preview() -> None:
        if preview_job is None or preview_job.status != "running" or preview_job.task is None:
            return
        preview_job.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await preview_job.task

    async def start_preview(request: Request) -> Response:
        nonlocal preview_job
        export, entire_range = preview_request(await request.json())
        services = await context.services()
        chat_id = export.conversation_ids[0]
        await services.catalog.get(chat_id)
        await cancel_active_preview()
        job = PreviewJob(id=uuid.uuid4().hex)

        def report(progress: ExportProgress) -> None:
            job.progress = progress

        async def run() -> None:
            try:
                job.result = await services.exports.preview(
                    export, entire_range=entire_range, progress=report
                )
                job.status = "complete"
            except asyncio.CancelledError:
                job.status = "cancelled"
            except ConnectorError as exc:
                job.status = "failed"
                job.error = {"message": str(exc), "kind": type(exc).__name__}

        job.task = asyncio.create_task(run())
        preview_job = job
        return JSONResponse({"id": job.id, "status": job.status}, status_code=202)

    def _preview_job(request: Request) -> PreviewJob:
        if preview_job is None or preview_job.id != request.path_params["job_id"]:
            raise NotFound("The preview was not found.")
        return preview_job

    async def preview_status(request: Request) -> Response:
        nonlocal preview_job
        job = _preview_job(request)
        body = {
            "id": job.id,
            "status": job.status,
            "progress": job.progress.model_dump(mode="json"),
            "error": job.error,
            "result": job.result.model_dump(mode="json") if job.result else None,
        }
        if job.status != "running":
            preview_job = None
        return JSONResponse(body)

    async def cancel_preview(request: Request) -> Response:
        job = _preview_job(request)
        if job.status == "running" and job.task is not None:
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job.task
        return JSONResponse({"id": job.id, "status": job.status}, status_code=202)

    def _job(request: Request) -> ExportJob:
        job = jobs.get(request.path_params["job_id"])
        if job is None:
            raise NotFound("The export was not found.")
        return job

    async def export_status(request: Request) -> Response:
        job = _job(request)
        return JSONResponse(
            {
                "id": job.id,
                "status": job.status,
                "progress": job.progress.model_dump(mode="json"),
                "error": job.error,
                "artifact": job.artifact.model_dump(mode="json") if job.artifact else None,
            }
        )

    async def cancel_export(request: Request) -> Response:
        job = _job(request)
        if job.status == "running" and job.task is not None:
            job.task.cancel()
        return JSONResponse({"id": job.id, "status": job.status}, status_code=202)

    async def export_file(request: Request) -> Response:
        job = _job(request)
        if job.status != "complete" or job.artifact is None:
            return JSONResponse({"error": "The export file is not ready."}, status_code=409)
        return FileResponse(
            job.artifact.path, media_type=job.artifact.content_type, filename=job.artifact.filename
        )

    async def heartbeat(_: Request) -> Response:
        return JSONResponse({"ok": True})

    routes = [
        Route("/", index),
        Route("/api/status", api(status)),
        Route("/api/chats", api(chats)),
        Route("/api/photos/{user_id}", api(photo)),
        Route("/api/preview", api(start_preview), methods=["POST"]),
        Route("/api/preview/{job_id}", api(preview_status)),
        Route("/api/preview/{job_id}/cancel", api(cancel_preview), methods=["POST"]),
        Route("/api/exports", api(start_export), methods=["POST"]),
        Route("/api/exports/{job_id}", api(export_status)),
        Route("/api/exports/{job_id}/cancel", api(cancel_export), methods=["POST"]),
        Route("/api/exports/{job_id}/file", api(export_file)),
        Route("/api/heartbeat", heartbeat, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(_guard(session_token, port, activity))])
