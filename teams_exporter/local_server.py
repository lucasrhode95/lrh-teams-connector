from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
import threading
import time as time_module
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from teams_exporter.teams_connector import (
    AuthenticationRequiredError,
    ChatSummary,
    ExtractionCancelled,
)
from teams_exporter.teams_exporter import TeamsExporter
from teams_exporter.teams_service import TeamsService


MAX_REQUEST_BYTES = 1_000_000
MAX_CHAT_CATALOG_SIZE = 10_000
MAX_MESSAGE_LIMIT = 1_000_000
MAX_PROFILE_IMAGE_BYTES = 2_000_000
DOWNLOAD_CHUNK_SIZE = 64 * 1024
LOCAL_IMAGE_ASSETS = {
    "avatar-person.svg",
    "chat-call.svg",
    "chat-group.svg",
}
LOCAL_STATIC_ASSETS = {"app.css", "app.js", "auth.js"}
TEMPLATE_DIRECTORY = Path(__file__).parent / "templates"
STATIC_DIRECTORY = Path(__file__).parent / "static"
TEMPLATE_ENVIRONMENT = Environment(
    loader=FileSystemLoader(TEMPLATE_DIRECTORY),
    autoescape=select_autoescape(("html",)),
)



@dataclass(frozen=True)
class DownloadRequest:
    chats: list[dict[str, Any]]
    since: datetime | None
    until: datetime | None
    limit: int | None
    concatenate: bool


@dataclass
class DownloadJob:
    request_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    finished_at: float | None = None
    status: str = "queued"
    progress: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None
    filename: str | None = None
    content_type: str | None = None
    output_path: Path | None = None
    artifact_directory: Path | None = None


class LocalTeamsHTTPServer(ThreadingHTTPServer):
    """Loopback server with background, observable export jobs."""

    daemon_threads = True
    block_on_close = False
    # HTTPServer enables SO_REUSEADDR by default. On Windows that can let two
    # copies of this local app bind the same loopback port, so requests may
    # reach an older process even after the user starts a new one.
    allow_reuse_address = False
    JOB_RETENTION_SECONDS = 60 * 60

    def __init__(self, server_address: tuple[str, int], service: TeamsService | None = None):
        self.service = service or TeamsService()
        self.exporter = TeamsExporter(
            self.service.connector,
            self.service.formatter,
            service=self.service,
        )
        self._jobs: dict[str, DownloadJob] = {}
        self._jobs_lock = threading.RLock()
        self._avatar_urls: dict[str, str] = {}
        self._avatar_lock = threading.RLock()
        super().__init__(server_address, LocalTeamsRequestHandler)

    def start_download(self, request: DownloadRequest) -> DownloadJob:
        with self._jobs_lock:
            self._prune_jobs_locked()
            if any(job.status in {"queued", "running"} for job in self._jobs.values()):
                raise RuntimeError("Another export is already running. Wait for it or cancel it first.")
            job = DownloadJob(request_id=uuid.uuid4().hex)
            self._jobs[job.request_id] = job
            worker = threading.Thread(
                target=self._run_download,
                args=(job, request),
                name=f"teams-export-{job.request_id[:8]}",
                daemon=True,
            )
            worker.start()
            return job

    def get_download_job(self, request_id: str) -> DownloadJob | None:
        with self._jobs_lock:
            self._prune_jobs_locked()
            return self._jobs.get(request_id)

    def register_avatar_uris(self, urls: list[str | None]) -> dict[str, str]:
        """Make authenticated Middle Tier picture URLs available by opaque local IDs."""
        can_fetch = getattr(self.service.connector, "can_fetch_profile_picture", None)
        registered: dict[str, str] = {}
        routes: dict[str, str] = {}
        if callable(can_fetch):
            for url in urls:
                if not isinstance(url, str) or not can_fetch(url):
                    continue
                key = hashlib.sha256(url.encode("utf-8")).hexdigest()
                registered[key] = url
                routes[url] = f"/api/profile-picture/{key}"
        with self._avatar_lock:
            self._avatar_urls = registered
        return routes

    def _run_download(self, job: DownloadJob, request: DownloadRequest) -> None:
        artifact_directory = Path(tempfile.mkdtemp(prefix="teams-export-job-"))
        with self._jobs_lock:
            job.artifact_directory = artifact_directory
            job.status = "running"

        def report_progress(update: dict[str, Any]) -> None:
            with self._jobs_lock:
                if job.status in {"queued", "running"}:
                    job.progress = {
                        key: value
                        for key, value in update.items()
                        if key in {
                            "phase",
                            "chat_index",
                            "chats_total",
                            "pages_fetched",
                            "messages_retrieved",
                            "messages_written",
                            "retry_count",
                        }
                    }

        try:
            filename, content_type, output_path = self._build_download(
                request,
                artifact_directory,
                cancel_event=job.cancel_event,
                progress_callback=report_progress,
            )
            if job.cancel_event.is_set():
                raise ExtractionCancelled("Export cancelled.")
            with self._jobs_lock:
                job.filename = filename
                job.content_type = content_type
                job.output_path = output_path
                job.status = "complete"
                job.finished_at = time_module.monotonic()
        except ExtractionCancelled:
            with self._jobs_lock:
                job.status = "cancelled"
                job.finished_at = time_module.monotonic()
            shutil.rmtree(artifact_directory, ignore_errors=True)
        except AuthenticationRequiredError as exc:
            with self._jobs_lock:
                job.status = "failed"
                job.error = {
                    "code": "authentication_required",
                    "message": str(exc),
                }
                job.finished_at = time_module.monotonic()
            shutil.rmtree(artifact_directory, ignore_errors=True)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            with self._jobs_lock:
                job.status = "failed"
                job.error = {"code": "invalid_request", "message": str(exc)}
                job.finished_at = time_module.monotonic()
            shutil.rmtree(artifact_directory, ignore_errors=True)
        except Exception as exc:
            with self._jobs_lock:
                job.status = "failed"
                job.error = {
                    "code": "export_failed",
                    "message": f"Teams export failed: {TeamsService._safe_error(exc)}",
                }
                job.finished_at = time_module.monotonic()
            shutil.rmtree(artifact_directory, ignore_errors=True)

    def _build_download(
        self,
        request: DownloadRequest,
        temp_dir: Path,
        *,
        cancel_event: threading.Event,
        progress_callback,
    ) -> tuple[str, str, Path]:
        self._raise_if_cancelled(cancel_event)
        if len(request.chats) == 1:
            chat = LocalTeamsRequestHandler._summary(request.chats[0])
            output_path = self.exporter.export_chat(
                chat,
                temp_dir / f"{self.exporter.filename_stem(chat)}.txt",
                limit=request.limit,
                since=request.since,
                until=request.until,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
            return output_path.name, "text/plain; charset=utf-8", output_path

        files_dir = temp_dir / "chats"
        files_dir.mkdir()
        exported = self.exporter.export_chats(
            [LocalTeamsRequestHandler._summary(chat) for chat in request.chats],
            files_dir,
            limit=request.limit,
            since=request.since,
            until=request.until,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        self._raise_if_cancelled(cancel_event)
        if request.concatenate:
            combined_path = temp_dir / "conversations.txt"
            with combined_path.open("wb") as combined:
                for index, path in enumerate(exported):
                    self._raise_if_cancelled(cancel_event)
                    if index:
                        combined.write(b"\n")
                    with path.open("rb") as source:
                        while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
                            self._raise_if_cancelled(cancel_event)
                            combined.write(chunk)
            return combined_path.name, "text/plain; charset=utf-8", combined_path

        archive_path = temp_dir / "teams-conversations.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in exported:
                self._raise_if_cancelled(cancel_event)
                with path.open("rb") as source, archive.open(path.name, "w") as target:
                    while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
                        self._raise_if_cancelled(cancel_event)
                        target.write(chunk)
        return archive_path.name, "application/zip", archive_path

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise ExtractionCancelled("Export cancelled.")

    def _prune_jobs_locked(self) -> None:
        now = time_module.monotonic()
        for request_id, job in list(self._jobs.items()):
            if (
                job.status not in {"queued", "running"}
                and job.finished_at is not None
                and now - job.finished_at > self.JOB_RETENTION_SECONDS
            ):
                if job.artifact_directory is not None:
                    shutil.rmtree(job.artifact_directory, ignore_errors=True)
                del self._jobs[request_id]


class LocalTeamsRequestHandler(BaseHTTPRequestHandler):
    server: LocalTeamsHTTPServer
    server_version = "TeamsExporter"
    sys_version = ""

    def do_GET(self) -> None:
        if not self._request_is_local():
            self._send_json(403, self._error("local_only", "This server accepts local browser requests only."))
            return
        parsed_target = urlsplit(self.path)
        path = parsed_target.path
        if path == "/":
            if getattr(self.server.service, "has_recent_connection", False):
                self._redirect_to_chats()
                return
            self._send_html_template("auth.html")
            return
        if self._get_path_requires_connection(path) and not self._check_connection(
            page_request=path == "/chats"
        ):
            return
        if path == "/chats":
            self._send_html_template("chats.html")
            return
        if path == "/api/auth-status":
            if self._check_connection():
                self._send_json(200, {"authenticated": True})
            return
        if path.startswith("/api/profile-picture/"):
            if not self._origin_is_local():
                self._send_json(403, self._error("origin_rejected", "The request origin is not allowed."))
                return
            image_key = path.removeprefix("/api/profile-picture/")
            if not re.fullmatch(r"[0-9a-f]{64}", image_key):
                self._send_json(404, self._error("not_found", "Not found."))
                return
            with self.server._avatar_lock:
                image_url = self.server._avatar_urls.get(image_key)
            if image_url is None:
                self._send_json(404, self._error("not_found", "Not found."))
                return
            try:
                image, content_type = self.server.service.get_profile_picture(image_url)
            except AuthenticationRequiredError as exc:
                self._send_json(401, self._error("authentication_required", str(exc)))
                return
            except Exception as exc:
                self._send_json(
                    502,
                    self._error(
                        "profile_picture_failed",
                        f"Could not load profile picture: {TeamsService._safe_error(exc)}",
                    ),
                )
                return
            if (
                not isinstance(image, bytes)
                or len(image) > MAX_PROFILE_IMAGE_BYTES
                or not isinstance(content_type, str)
                or not content_type.startswith("image/")
            ):
                self._send_json(502, self._error("profile_picture_failed", "Teams returned an invalid image."))
                return
            self.send_response(200)
            self._security_headers(content_type)
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)
            return
        if path.startswith("/static/"):
            asset_name = path.removeprefix("/static/")
            if asset_name not in LOCAL_STATIC_ASSETS:
                self._send_json(404, self._error("not_found", "Not found."))
                return
            asset_path = STATIC_DIRECTORY / asset_name
            try:
                body = asset_path.read_bytes()
            except OSError:
                self._send_json(404, self._error("not_found", "Not found."))
                return
            content_type = "text/css; charset=utf-8" if asset_name.endswith(".css") else "text/javascript; charset=utf-8"
            self.send_response(200)
            self._security_headers(content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/chats":
            self._list_chats()
            return
        if path == "/api/download-status":
            request_id = self._query_request_id(parsed_target.query)
            if request_id is None:
                self._send_json(400, self._error("invalid_request", "A valid request_id is required."))
                return
            job = self.server.get_download_job(request_id)
            if job is None:
                self._send_json(404, self._error("not_found", "The export request was not found."))
                return
            with self.server._jobs_lock:
                result = {
                    "request_id": job.request_id,
                    "status": job.status,
                    "progress": dict(job.progress),
                    "download_ready": job.status == "complete",
                    "error": dict(job.error) if job.error else None,
                }
            self._send_json(200, result)
            return
        if path == "/api/download-file":
            request_id = self._query_request_id(parsed_target.query)
            if request_id is None:
                self._send_json(400, self._error("invalid_request", "A valid request_id is required."))
                return
            job = self.server.get_download_job(request_id)
            if job is None:
                self._send_json(404, self._error("not_found", "The export request was not found."))
                return
            with self.server._jobs_lock:
                if job.status != "complete" or job.output_path is None or job.filename is None:
                    self._send_json(409, self._error("export_not_ready", "The export file is not ready."))
                    return
                path_to_send = job.output_path
                filename = job.filename
                content_type = job.content_type or "application/octet-stream"
            if not path_to_send.is_file():
                self._send_json(410, self._error("export_expired", "The export file is no longer available."))
                return
            self._send_file(path_to_send, filename, content_type)
            return
        if path.startswith("/assets/"):
            asset_name = path.removeprefix("/assets/")
            if asset_name not in LOCAL_IMAGE_ASSETS:
                self._send_json(404, self._error("not_found", "Not found."))
                return
            asset_path = Path(__file__).parent / asset_name
            try:
                body = asset_path.read_bytes()
            except OSError:
                self._send_json(404, self._error("not_found", "Not found."))
                return
            self.send_response(200)
            self._security_headers("image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, self._error("not_found", "Not found."))

    def _send_html_template(self, template_name: str) -> None:
        body = TEMPLATE_ENVIRONMENT.get_template(template_name).render().encode("utf-8")
        self.send_response(200)
        self._security_headers("text/html; charset=utf-8")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self' https:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _get_path_requires_connection(path: str) -> bool:
        return path in {
            "/chats",
            "/api/chats",
            "/api/download-status",
            "/api/download-file",
        } or path.startswith("/api/profile-picture/")

    def _check_connection(self, *, page_request: bool = False) -> bool:
        try:
            self.server.service.check_authentication()
            return True
        except AuthenticationRequiredError as exc:
            self._invalidate_service_authentication()
            if page_request:
                self._redirect_to_authentication()
            else:
                self._send_json(401, self._error("authentication_required", str(exc)))
            return False
        except Exception as exc:
            if page_request:
                self._redirect_to_authentication()
            else:
                self._send_json(
                    502,
                    self._error(
                        "connection_failed",
                        f"Could not confirm the Teams connection: {TeamsService._safe_error(exc)}",
                    ),
                )
            return False

    def _invalidate_service_authentication(self) -> None:
        invalidate = getattr(self.server.service, "invalidate_authentication", None)
        if callable(invalidate):
            invalidate()

    def _redirect_to_authentication(self) -> None:
        self.send_response(303)
        self._security_headers("text/plain; charset=utf-8")
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_to_chats(self) -> None:
        self.send_response(303)
        self._security_headers("text/plain; charset=utf-8")
        self.send_header("Location", "/chats")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if not self._request_is_local():
            self._send_json(403, self._error("local_only", "This server accepts local browser requests only."))
            return
        if not self._origin_is_local():
            self._send_json(403, self._error("origin_rejected", "The request origin is not allowed."))
            return
        path = urlsplit(self.path).path
        if path not in {"/api/download", "/api/cancel"}:
            self._send_json(404, self._error("not_found", "Not found."))
            return
        if not self._check_connection():
            return
        try:
            payload = self._read_json_body()
            if path == "/api/cancel":
                if not isinstance(payload, dict):
                    raise ValueError("The request body must be a JSON object.")
                request_id = payload.get("request_id")
                if not self._valid_request_id(request_id):
                    raise ValueError("A valid request_id is required.")
                job = self.server.get_download_job(request_id)
                if job is None:
                    self._send_json(404, self._error("not_found", "The export request was not found."))
                    return
                with self.server._jobs_lock:
                    active = job.status in {"queued", "running"}
                    if active:
                        job.cancel_event.set()
                self._send_json(
                    202 if active else 200,
                    {"request_id": request_id, "cancellation_requested": active, "status": job.status},
                )
                return

            request = self._prepare_download(payload)
            try:
                job = self.server.start_download(request)
            except RuntimeError as exc:
                self._send_json(409, self._error("export_busy", str(exc)))
                return
            self._send_json(202, {"request_id": job.request_id, "status": job.status})
        except AuthenticationRequiredError as exc:
            self._send_json(401, self._error("authentication_required", str(exc)))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, self._error("invalid_request", str(exc)))
        except Exception as exc:
            self._send_json(
                502,
                self._error("export_failed", f"Teams export failed: {TeamsService._safe_error(exc)}"),
            )

    def log_message(self, format: str, *args: Any) -> None:
        # Request bodies and chat names can contain private information; keep the
        # simple local server quiet rather than writing access logs by default.
        return

    def _list_chats(self) -> None:
        try:
            chats: list[dict[str, Any]] = []
            page_token = None
            seen_tokens: set[str] = set()
            while True:
                result = self.server.service.list_chats(
                    page_size=100,
                    page_token=page_token,
                    include_avatar_uris=True,
                )
                chats.extend(result["items"])
                if len(chats) > MAX_CHAT_CATALOG_SIZE:
                    raise RuntimeError("Chat catalog exceeds the local UI size limit.")
                next_token = result.get("next_page_token")
                if next_token is None:
                    break
                if not isinstance(next_token, str) or next_token in seen_tokens:
                    raise RuntimeError("Teams returned an invalid chat-list continuation.")
                seen_tokens.add(next_token)
                page_token = next_token
            account = getattr(self.server.service, "account_info", None)
            account_payload = dict(account) if isinstance(account, dict) else account
            avatar_uris = [chat.get("avatar_url") for chat in chats]
            if isinstance(account_payload, dict):
                avatar_uris.append(account_payload.get("picture_url"))
            avatar_routes = self.server.register_avatar_uris(avatar_uris)
            for chat in chats:
                source_uri = chat.get("avatar_url")
                if source_uri in avatar_routes:
                    chat["avatar_url"] = avatar_routes[source_uri]
            if isinstance(account_payload, dict):
                source_uri = account_payload.get("picture_url")
                if source_uri in avatar_routes:
                    account_payload["picture_url"] = avatar_routes[source_uri]
            self._send_json(
                200,
                {
                    "chats": chats,
                    "count": len(chats),
                    "database_notice": getattr(self.server.service, "database_notice", None),
                    "account": account_payload,
                },
            )
        except AuthenticationRequiredError as exc:
            self._send_json(401, self._error("authentication_required", str(exc)))
        except Exception as exc:
            self._send_json(
                502,
                self._error("catalog_failed", f"Could not load chats: {TeamsService._safe_error(exc)}"),
            )

    def _prepare_download(self, payload: Any) -> DownloadRequest:
        if not isinstance(payload, dict):
            raise ValueError("The request body must be a JSON object.")
        chat_ids = payload.get("chat_ids")
        if not isinstance(chat_ids, list) or not chat_ids or any(
            not isinstance(chat_id, str) or not chat_id.strip() for chat_id in chat_ids
        ):
            raise ValueError("Select one or more conversations.")
        chat_ids = list(dict.fromkeys(chat_id.strip() for chat_id in chat_ids))

        from_date_value = payload.get("from_date")
        to_date_value = payload.get("to_date")
        since = self._parse_local_date(
            from_date_value,
            utc_offset_minutes=payload.get("from_utc_offset_minutes"),
        )
        until = self._parse_local_date(
            to_date_value,
            end_of_day=True,
            utc_offset_minutes=payload.get("to_utc_offset_minutes"),
        )
        if since is not None and until is not None and since > until:
            raise ValueError("from_date must be earlier than or equal to to_date.")
        limit = self._parse_message_limit(payload.get("message_limit"))
        concatenate = payload.get("concatenate", False)
        if not isinstance(concatenate, bool):
            raise ValueError("concatenate must be true or false.")

        chats = self._all_chats()
        by_id = {chat["id"]: chat for chat in chats}
        missing = [chat_id for chat_id in chat_ids if chat_id not in by_id]
        if missing:
            raise ValueError("One or more selected chats are no longer available. Refresh the list and try again.")
        selected_chats = [by_id[chat_id] for chat_id in chat_ids]

        return DownloadRequest(
            chats=selected_chats,
            since=since,
            until=until,
            limit=limit,
            concatenate=concatenate,
        )

    @staticmethod
    def _valid_request_id(value: Any) -> bool:
        if not isinstance(value, str) or len(value) != 32:
            return False
        try:
            return uuid.UUID(hex=value).hex == value
        except ValueError:
            return False

    @classmethod
    def _query_request_id(cls, query: str) -> str | None:
        values = parse_qs(query, keep_blank_values=True).get("request_id", [])
        if len(values) != 1 or not cls._valid_request_id(values[0]):
            return None
        return values[0]

    def _all_chats(self) -> list[dict[str, Any]]:
        chats: list[dict[str, Any]] = []
        page_token = None
        seen_tokens: set[str] = set()
        while True:
            result = self.server.service.list_chats(page_size=100, page_token=page_token)
            chats.extend(result["items"])
            if len(chats) > MAX_CHAT_CATALOG_SIZE:
                raise ValueError("Chat catalog exceeds the local UI size limit.")
            next_token = result.get("next_page_token")
            if next_token is None:
                return chats
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise RuntimeError("Teams returned an invalid chat-list continuation.")
            seen_tokens.add(next_token)
            page_token = next_token

    @staticmethod
    def _summary(record: dict[str, Any]) -> ChatSummary:
        timestamp = record.get("last_message_at")
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
        return ChatSummary(
            id=record["id"],
            display_name=record["name"],
            last_message_at=parsed,
            chat_type=record.get("chat_type"),
            is_self_chat=bool(record.get("is_self_chat")),
        )

    @staticmethod
    def _parse_local_date(
        value: Any,
        *,
        end_of_day: bool = False,
        utc_offset_minutes: Any = None,
    ) -> datetime | None:
        field_name = "to_date" if end_of_day else "from_date"
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a calendar date.")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must use YYYY-MM-DD format.") from exc
        if utc_offset_minutes is None:
            local_time = time.max if end_of_day else time.min
            return datetime.combine(parsed, local_time).astimezone()
        if (
            isinstance(utc_offset_minutes, bool)
            or not isinstance(utc_offset_minutes, int)
            or not -840 <= utc_offset_minutes <= 840
        ):
            raise ValueError("UTC offset must be a whole number of minutes from -840 to 840.")
        local_offset = timezone(timedelta(minutes=-utc_offset_minutes))
        local_time = time.max if end_of_day else time.min
        return datetime.combine(parsed, local_time, tzinfo=local_offset)

    @staticmethod
    def _parse_message_limit(value: Any) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise ValueError("message_limit must be a positive whole number or blank.")
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
            value = int(value.strip())
        if not isinstance(value, int) or value < 1 or value > MAX_MESSAGE_LIMIT:
            raise ValueError(f"message_limit must be between 1 and {MAX_MESSAGE_LIMIT}, or blank.")
        return value

    def _read_json_body(self) -> Any:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ValueError("Content-Length is required.") from exc
        if length < 0:
            raise ValueError("Content-Length must not be negative.")
        if length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _request_is_local(self) -> bool:
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("//" + host)
            return (
                parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.port in {None, self.server.server_port}
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            return False

    def _origin_is_local(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
            return (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.port == self.server.server_port
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            return False

    def _send_file(self, path: Path, filename: str, content_type: str) -> None:
        fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "teams-export"
        disposition = (
            f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )
        self.send_response(200)
        self._security_headers(content_type)
        self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, DOWNLOAD_CHUNK_SIZE)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"error": {"code": code, "message": message}}


def serve_local_ui(port: int = 8765) -> None:
    """Serve the local export UI on loopback until interrupted."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535.")
    server = LocalTeamsHTTPServer(("127.0.0.1", port))
    print(f"Teams export UI: http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
