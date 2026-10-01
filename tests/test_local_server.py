from __future__ import annotations

import http.client
import json
import threading
import time as time_module
import unittest
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from io import BytesIO
from unittest.mock import patch

from teams_exporter.local_server import (
    LocalTeamsHTTPServer,
    LocalTeamsRequestHandler,
)
from teams_exporter.teams_connector import (
    AuthenticationRequiredError,
    ChatSummary,
    ExtractionCancelled,
    TeamsConnector,
)
from teams_exporter.teams_message_formatter import TeamsMessageFormatter


class ExportConnector:
    def __init__(self) -> None:
        self.chats = {
            "19:alpha@thread.v2": [
                self._message("a0", "2026-09-27T23:30:00Z", "Before the date"),
                self._message("a1", "2026-09-28T10:00:00Z", "Alpha earlier"),
                self._message("a2", "2026-09-28T11:00:00Z", "Alpha latest"),
            ],
            "19:beta@thread.v2": [
                self._message("b1", "2026-09-28T12:00:00Z", "Beta message", "Jordan"),
            ],
        }
        self.calls = []
        self.block_after_first_page = False
        self.waiting_for_cancel = threading.Event()
        self.resume = threading.Event()
        self.last_message_read_coverage = {"retry_count": 0}

    @staticmethod
    def can_fetch_profile_picture(url):
        return url.startswith("https://images.example.test/")

    @staticmethod
    def _message(message_id: str, timestamp: str, content: str, author: str = "Taylor"):
        return {
            "id": message_id,
            "originalarrivaltime": timestamp,
            "imdisplayname": author,
            "messagetype": "Text",
            "content": content,
        }

    def iter_message_pages(
        self,
        conversation_id,
        *,
        limit=None,
        since=None,
        until=None,
        cancel_event=None,
        progress_callback=None,
    ):
        self.calls.append((conversation_id, limit, since, until))
        messages = self.chats[conversation_id]
        if since is not None:
            messages = [m for m in messages if TeamsConnector._message_datetime(m) >= since]
        if until is not None:
            messages = [m for m in messages if TeamsConnector._message_datetime(m) <= until]
        if limit is not None:
            messages = messages[-limit:]
        pages = []
        end = len(messages)
        while end:
            start = max(0, end - 2)
            pages.append(messages[start:end])
            end = start
        pages_fetched = 0
        messages_retrieved = 0
        for page_index, page in enumerate(pages):
            if page_index == 1 and self.block_after_first_page:
                self.waiting_for_cancel.set()
                while not self.resume.wait(0.01):
                    if cancel_event is not None and cancel_event.is_set():
                        raise ExtractionCancelled("cancelled")
            if cancel_event is not None and cancel_event.is_set():
                raise ExtractionCancelled("cancelled")
            pages_fetched += 1
            messages_retrieved += len(page)
            if progress_callback is not None:
                progress_callback(pages_fetched, messages_retrieved, 0)
            yield page
        self.last_message_read_coverage = {
            "retry_count": 0,
            "missing_timestamp_messages_excluded": 0,
        }


class ExportService:
    def __init__(self) -> None:
        self.connector = ExportConnector()
        self.formatter = TeamsMessageFormatter()
        self.auth_required = False
        self.account_info = {
            "name": "Taylor Reed",
            "email": "taylor@example.test",
            "picture_url": "https://images.example.test/taylor.png",
        }
        self.database_notice = None
        self.chats = [
            ChatSummary(
                "19:alpha@thread.v2",
                "Alpha",
                chat_type="group",
                avatar_url="https://images.example.test/alpha.png",
            ),
            ChatSummary("19:beta@thread.v2", "Beta", chat_type="oneOnOne"),
        ]
        self.list_calls = []
        self.refresh_calls = []
        self.cached_list_calls = []
        self.archive: dict[str, dict[str, dict]] = {}
        self.picture_calls = []
        self.auth_checks = 0
        self.connection_recent = False
        self.connection_error = None

    @property
    def has_recent_connection(self):
        return self.connection_recent

    def list_chats(self, *, page_size=50, page_token=None, include_avatar_uris=False, refresh=False):
        if self.auth_required:
            raise AuthenticationRequiredError("not signed in")
        self.list_calls.append((page_size, page_token, include_avatar_uris))
        if refresh:
            self.refresh_calls.append(True)
        offset = int(page_token.split(":")[1]) if page_token else 0
        selected = self.chats[offset : offset + page_size]
        next_offset = offset + len(selected)
        return {
            "items": [
                {
                    "id": chat.id,
                    "name": chat.display_name,
                    "chat_type": chat.chat_type,
                    "last_message_at": None,
                    **({"avatar_url": chat.avatar_url} if include_avatar_uris else {}),
                }
                for chat in selected
            ],
            "next_page_token": f"offset:{next_offset}" if next_offset < len(self.chats) else None,
        }

    def list_cached_chats(self, *, page_size=50, page_token=None, include_avatar_uris=False):
        self.cached_list_calls.append((page_size, page_token, include_avatar_uris))
        return self.list_chats(
            page_size=page_size,
            page_token=page_token,
            include_avatar_uris=include_avatar_uris,
        )

    def check_authentication(self):
        self.auth_checks += 1
        if self.connection_error is not None:
            raise self.connection_error
        if self.auth_required:
            raise AuthenticationRequiredError("Teams sign-in is required.")
        return {"authenticated": True}

    def get_profile_picture(self, url):
        if self.auth_required:
            raise AuthenticationRequiredError("not signed in")
        self.picture_calls.append(url)
        return b"sanitized-test-image", "image/png"

    def _messages_in_range(self, conversation_id, *, since=None, until=None):
        messages = list(self.archive.get(conversation_id, {}).values())
        if since is not None:
            messages = [
                message
                for message in messages
                if TeamsConnector._message_datetime(message) >= since
            ]
        if until is not None:
            messages = [
                message
                for message in messages
                if TeamsConnector._message_datetime(message) <= until
            ]
        return sorted(
            messages,
            key=lambda message: (
                TeamsConnector._message_datetime(message),
                message["id"],
            ),
        )

    def fetch_chat_to_archive(
        self,
        conversation_id,
        *,
        since=None,
        until=None,
        cancel_event=None,
        progress_callback=None,
    ):
        archived = self.archive.setdefault(conversation_id, {})
        for page in self.connector.iter_message_pages(
            conversation_id,
            limit=None,
            since=since,
            until=until,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        ):
            for message in page:
                archived[message["id"]] = {
                    **message,
                    "__formatted_body": self.formatter.format_message(message),
                }

    def archived_export_message_count(
        self, conversation_id, *, since=None, until=None, limit=None
    ):
        messages = self._messages_in_range(conversation_id, since=since, until=until)
        if limit is not None:
            messages = messages[-limit:]
        cutoff = None
        if limit is not None and messages:
            first = messages[0]
            timestamp = TeamsConnector._message_datetime(first).isoformat().replace(
                "+00:00", "Z"
            )
            cutoff = (timestamp, first["id"])
        return len(messages), cutoff

    def iter_archived_export_pages(
        self, conversation_id, *, since=None, until=None, from_key=None, page_size=500
    ):
        messages = self._messages_in_range(conversation_id, since=since, until=until)
        if from_key is not None:
            messages = [
                message
                for message in messages
                if (
                    TeamsConnector._message_datetime(message)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    message["id"],
                )
                >= from_key
            ]
        for offset in range(0, len(messages), page_size):
            yield messages[offset : offset + page_size]


class LocalTeamsServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ExportService()
        self.server = LocalTeamsHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_client_disconnect_during_response_does_not_escape_handler(self):
        handler = object.__new__(LocalTeamsRequestHandler)
        with patch.object(
            BaseHTTPRequestHandler,
            "handle",
            side_effect=ConnectionAbortedError("client disconnected"),
        ):
            handler.handle()

        with patch.object(
            BaseHTTPRequestHandler,
            "handle",
            side_effect=RuntimeError("unexpected handler failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected handler failure"):
                handler.handle()

    def test_duplicate_server_instance_cannot_share_the_same_port(self):
        with self.assertRaises(OSError):
            LocalTeamsHTTPServer(
                ("127.0.0.1", self.server.server_port),
                self.service,
            )

    def request(self, method: str, path: str, payload=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = dict(extra_headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
            headers.setdefault("Content-Length", str(len(body)))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def download_request(self, payload):
        status, _, body = self.request("POST", "/api/download", payload)
        self.assertEqual(status, 202, body.decode("utf-8"))
        request_id = json.loads(body)["request_id"]
        deadline = time_module.monotonic() + 5
        while time_module.monotonic() < deadline:
            status, _, body = self.request(
                "GET", f"/api/download-status?request_id={request_id}"
            )
            self.assertEqual(status, 200)
            job = json.loads(body)
            if job["status"] == "complete":
                return self.request(
                    "GET", f"/api/download-file?request_id={request_id}"
                )
            if job["status"] in {"failed", "cancelled"}:
                self.fail(f"export ended as {job['status']}: {job.get('error')}")
            time_module.sleep(0.01)
        self.fail("export did not finish before test timeout")

    def test_page_and_chat_api_are_local_and_typed(self):
        self.service.database_notice = "An unverified archive was preserved."
        page_status, page_headers, page = self.request("GET", "/")
        self.assertEqual(page_status, 200)
        self.assertIn(b"Checking Teams sign-in", page)
        self.assertIn(b"/static/auth.js", page)
        self.assertIn(b"Confirming the saved Teams sign-in", page)
        self.assertEqual(self.service.list_calls, [])

        status, _, body = self.request("GET", "/api/auth-status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"authenticated": True})
        chats_status, page_headers, page = self.request("GET", "/chats")
        self.assertEqual(chats_status, 200)
        self.assertIn(b"Download conversations", page)
        self.assertIn(b'href="/static/app.css"', page)
        self.assertIn(b'src="/static/app.js"', page)
        self.assertIn(b"Loading conversations", page)
        self.assertIn(b"class=\"spinner\"", page)
        self.assertEqual(page_headers["Content-Security-Policy"].split(";", 1)[0], "default-src 'none'")
        self.assertIn("img-src 'self' https:", page_headers["Content-Security-Policy"])
        self.assertIn("style-src 'self'; script-src 'self'", page_headers["Content-Security-Policy"])

        status, headers, body = self.request("GET", "/api/chats")
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["account"]["name"], self.service.account_info["name"])
        self.assertEqual(result["account"]["email"], self.service.account_info["email"])
        self.assertEqual(result["database_notice"], self.service.database_notice)
        self.assertEqual([chat["name"] for chat in result["chats"]], ["Alpha", "Beta"])
        self.assertRegex(result["chats"][0]["avatar_url"], r"^/api/profile-picture/[0-9a-f]{64}$")
        self.assertRegex(result["account"]["picture_url"], r"^/api/profile-picture/[0-9a-f]{64}$")
        self.assertNotIn("images.example.test", body.decode("utf-8"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.service.list_calls, [(100, None, True)])

        image_status, _, image_body = self.request(
            "GET",
            result["chats"][0]["avatar_url"],
            extra_headers={"Origin": "https://remote.example.test"},
        )
        self.assertEqual(image_status, 403)
        self.assertEqual(self.service.picture_calls, [])

        image_status, image_headers, image_body = self.request("GET", result["chats"][0]["avatar_url"])
        self.assertEqual(image_status, 200)
        self.assertEqual(image_headers["Content-Type"], "image/png")
        self.assertEqual(image_headers["Cache-Control"], "no-store")
        self.assertEqual(image_body, b"sanitized-test-image")
        self.assertEqual(self.service.picture_calls, ["https://images.example.test/alpha.png"])

    def test_chat_refresh_endpoint_forwards_refresh_and_exposes_sqlite_catalog(self):
        status, _, body = self.request("GET", "/api/chats?refresh=true")
        self.assertEqual(status, 200)
        self.assertEqual(self.service.refresh_calls, [True])
        self.assertEqual(json.loads(body)["source"], "teams")

        status, _, body = self.request("GET", "/api/cached-chats")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["source"], "cache")
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.service.cached_list_calls, [(100, None, True)])

    def test_auth_landing_explains_sign_in_and_gates_the_chat_page(self):
        self.service.auth_required = True
        status, _, body = self.request("GET", "/api/auth-status")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "authentication_required")

        status, headers, body = self.request("GET", "/chats")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertEqual(body, b"")

        status, _, body = self.request("GET", "/")
        self.assertIn(b"Checking Teams sign-in", body)
        self.assertIn(b"/static/auth.js", body)
        status, _, body = self.request("GET", "/static/auth.js")
        self.assertEqual(status, 200)
        self.assertIn(b"python -m teams_exporter auth", body)

    def test_recent_connection_skips_the_landing_page(self):
        self.service.connection_recent = True

        status, headers, _ = self.request("GET", "/")

        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/chats")
        self.assertEqual(self.service.auth_checks, 0)

    def test_connection_guard_stops_api_request_before_loading_chats(self):
        self.service.connection_error = ConnectionError("name resolution failed")

        status, _, body = self.request("GET", "/api/chats")

        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["code"], "connection_failed")
        self.assertIn("name resolution failed", json.loads(body)["error"]["message"])
        self.assertEqual(self.service.list_calls, [])

    def test_local_avatar_assets_are_available_and_unknown_assets_are_rejected(self):
        for asset in ("avatar-person.svg", "chat-call.svg", "chat-group.svg"):
            with self.subTest(asset=asset):
                status, headers, body = self.request("GET", f"/assets/{asset}")
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith("image/svg+xml"))
                self.assertIn(b"<svg", body)

        status, _, body = self.request("GET", "/assets/not-an-avatar.svg")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_profile_picture_route_only_serves_registered_ids(self):
        status, _, body = self.request("GET", "/api/profile-picture/" + ("a" * 64))
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

        status, _, body = self.request("GET", "/api/profile-picture/invalid")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_profile_picture_route_reports_expired_authentication(self):
        status, _, body = self.request("GET", "/api/chats")
        self.assertEqual(status, 200)
        picture_path = json.loads(body)["chats"][0]["avatar_url"]
        self.service.auth_required = True

        status, _, body = self.request("GET", picture_path)

        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "authentication_required")
        self.assertEqual(self.service.picture_calls, [])

    def test_page_static_assets_are_available_and_unknown_files_are_rejected(self):
        for asset, content_type in (
            ("app.css", "text/css"),
            ("app.js", "text/javascript"),
            ("auth.js", "text/javascript"),
        ):
            with self.subTest(asset=asset):
                status, headers, body = self.request("GET", f"/static/{asset}")
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(content_type))
                self.assertGreater(len(body), 0)

        status, _, body = self.request("GET", "/static/not-an-asset.js")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_chat_catalog_continues_through_bounded_pages(self):
        self.service.chats = [
            ChatSummary(f"19:chat-{index}@thread.v2", f"Chat {index}")
            for index in range(205)
        ]
        status, _, body = self.request("GET", "/api/chats")
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["count"], 205)
        self.assertEqual(
            self.service.list_calls,
            [
                (100, None, True),
                (100, "offset:100", True),
                (100, "offset:200", True),
            ],
        )

    def test_chat_catalog_error_shows_safe_underlying_connection_detail(self):
        original_list_chats = self.service.list_chats

        def fail_list_chats(**_kwargs):
            raise ConnectionError("test name-resolution failure")

        self.service.list_chats = fail_list_chats
        try:
            status, _, body = self.request("GET", "/api/chats")
        finally:
            self.service.list_chats = original_list_chats

        result = json.loads(body)
        self.assertEqual(status, 502)
        self.assertIn(
            "ConnectionError: test name-resolution failure",
            result["error"]["message"],
        )

    def test_single_chat_download_applies_local_date_and_latest_message_limit(self):
        status, headers, body = self.download_request(
            {
                "chat_ids": ["19:alpha@thread.v2"],
                "from_date": "2026-09-28",
                "message_limit": "1",
                "concatenate": False,
            },
        )
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("filename*=UTF-8''Alpha", headers["Content-Disposition"])
        self.assertIn("Messages: 1", text)
        self.assertIn("Alpha latest", text)
        self.assertNotIn("Alpha earlier", text)
        self.assertNotIn("Before the date", text)
        chat_id, limit, since, until = self.service.connector.calls[-1]
        self.assertEqual((chat_id, limit, until), ("19:alpha@thread.v2", None, None))
        self.assertEqual(since.date().isoformat(), "2026-09-28")
        self.assertIsNotNone(since.utcoffset())

    def test_multiple_chats_download_as_zip_with_one_text_per_chat(self):
        status, headers, body = self.download_request(
            {"chat_ids": ["19:beta@thread.v2", "19:alpha@thread.v2"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        with zipfile.ZipFile(BytesIO(body)) as archive:
            self.assertEqual(len(archive.namelist()), 2)
            texts = [archive.read(name).decode("utf-8") for name in archive.namelist()]
        self.assertTrue(any("Conversation: Beta" in text for text in texts))
        self.assertTrue(any("Conversation: Alpha" in text for text in texts))
        self.assertTrue(all(name.endswith(".txt") for name in archive.namelist()))
        self.assertTrue(all(call[1] is None for call in self.service.connector.calls))

    def test_combined_chats_download_as_plain_text_in_selection_order(self):
        status, headers, body = self.download_request(
            {
                "chat_ids": ["19:beta@thread.v2", "19:alpha@thread.v2"],
                "concatenate": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("filename*=UTF-8''conversations.txt", headers["Content-Disposition"])
        text = body.decode("utf-8")
        self.assertLess(text.index("Conversation: Beta"), text.index("Conversation: Alpha"))

    def test_date_range_is_inclusive_and_uses_the_browser_timezone(self):
        status, _, body = self.download_request(
            {
                "chat_ids": ["19:alpha@thread.v2"],
                "from_date": "2026-09-28",
                "to_date": "2026-09-28",
                "from_utc_offset_minutes": 180,
                "to_utc_offset_minutes": 180,
                "message_limit": None,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("Alpha earlier", body.decode("utf-8"))
        self.assertIn("Alpha latest", body.decode("utf-8"))
        self.assertNotIn("Before the date", body.decode("utf-8"))
        chat_id, limit, since, until = self.service.connector.calls[-1]
        expected_tz = timezone(timedelta(hours=-3))
        self.assertEqual(chat_id, "19:alpha@thread.v2")
        self.assertIsNone(limit)
        self.assertEqual(since, datetime(2026, 9, 28, 0, 0, tzinfo=expected_tz))
        self.assertEqual(
            until,
            datetime.combine(date.fromisoformat("2026-09-28"), time.max, expected_tz),
        )

    def test_reversed_date_range_is_rejected_before_export(self):
        status, _, body = self.request(
            "POST",
            "/api/download",
            {
                "chat_ids": ["19:alpha@thread.v2"],
                "from_date": "2026-09-29",
                "to_date": "2026-09-28",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
        self.assertEqual(self.service.connector.calls, [])

    def test_download_progress_and_cancel_preserve_completed_page_counts(self):
        connector = self.service.connector
        connector.block_after_first_page = True
        status, _, body = self.request(
            "POST", "/api/download", {"chat_ids": ["19:alpha@thread.v2"]}
        )
        self.assertEqual(status, 202)
        request_id = json.loads(body)["request_id"]
        self.assertTrue(connector.waiting_for_cancel.wait(2))

        status, _, body = self.request(
            "GET", f"/api/download-status?request_id={request_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["progress"]["pages_fetched"], 1)

        status, _, body = self.request("POST", "/api/cancel", {"request_id": request_id})
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(body)["cancellation_requested"])

        deadline = time_module.monotonic() + 3
        while time_module.monotonic() < deadline:
            status, _, body = self.request(
                "GET", f"/api/download-status?request_id={request_id}"
            )
            job = json.loads(body)
            if job["status"] == "cancelled":
                break
            time_module.sleep(0.01)
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["progress"]["pages_fetched"], 1)

    def test_download_routes_reject_invalid_and_unknown_request_ids(self):
        for path, expected_status in (
            ("/api/download-status?request_id=../secret", 400),
            ("/api/download-file?request_id=00000000000000000000000000000000", 404),
        ):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, expected_status)
                self.assertIn(
                    json.loads(body)["error"]["code"], {"invalid_request", "not_found"}
                )

    def test_authentication_needed_and_invalid_origin_are_clear(self):
        self.service.auth_required = True
        status, _, body = self.request("GET", "/api/chats")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "authentication_required")

        self.service.auth_required = False
        status, _, body = self.request(
            "POST",
            "/api/download",
            {"chat_ids": ["19:alpha@thread.v2"]},
            {"Origin": "https://example.test"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "origin_rejected")
        self.assertEqual(self.service.connector.calls, [])

        status, _, body = self.request(
            "GET", "/api/chats", extra_headers={"Host": "untrusted.example"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "local_only")

    def test_unknown_chat_and_invalid_limit_are_rejected(self):
        status, _, body = self.request(
            "POST", "/api/download", {"chat_ids": ["unknown-chat"]}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")

        for invalid_limit in ("0", 0, "-1", -1, "1.5", 1.5, True, "1e3"):
            with self.subTest(message_limit=invalid_limit):
                status, _, body = self.request(
                    "POST",
                    "/api/download",
                    {"chat_ids": ["19:alpha@thread.v2"], "message_limit": invalid_limit},
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
        self.assertEqual(self.service.connector.calls, [])


if __name__ == "__main__":
    unittest.main()
