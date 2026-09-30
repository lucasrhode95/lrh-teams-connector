from __future__ import annotations

import http.client
import json
import threading
import unittest
import zipfile
from io import BytesIO

from teams_exporter.local_server import LocalTeamsHTTPServer
from teams_exporter.teams_connector import AuthenticationRequiredError, ChatSummary, TeamsConnector
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

    @staticmethod
    def _message(message_id: str, timestamp: str, content: str, author: str = "Taylor"):
        return {
            "id": message_id,
            "originalarrivaltime": timestamp,
            "imdisplayname": author,
            "messagetype": "Text",
            "content": content,
        }

    def get_messages(self, conversation_id, *, limit=None, since=None, until=None):
        self.calls.append((conversation_id, limit, since, until))
        messages = self.chats[conversation_id]
        if since is not None:
            messages = [m for m in messages if TeamsConnector._message_datetime(m) >= since]
        if until is not None:
            messages = [m for m in messages if TeamsConnector._message_datetime(m) <= until]
        if limit is not None:
            messages = messages[-limit:]
        return messages


class ExportService:
    def __init__(self) -> None:
        self.connector = ExportConnector()
        self.formatter = TeamsMessageFormatter()
        self.auth_required = False
        self.chats = [
            ChatSummary("19:alpha@thread.v2", "Alpha", chat_type="group"),
            ChatSummary("19:beta@thread.v2", "Beta", chat_type="oneOnOne"),
        ]
        self.list_calls = []

    def list_chats(self, *, page_size=50, page_token=None):
        if self.auth_required:
            raise AuthenticationRequiredError("not signed in")
        self.list_calls.append((page_size, page_token))
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
                }
                for chat in selected
            ],
            "next_page_token": f"offset:{next_offset}" if next_offset < len(self.chats) else None,
        }


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

    def test_page_and_chat_api_are_local_and_typed(self):
        page_status, page_headers, page = self.request("GET", "/")
        self.assertEqual(page_status, 200)
        self.assertIn(b"Download conversations", page)
        self.assertEqual(page_headers["Content-Security-Policy"].split(";", 1)[0], "default-src 'none'")

        status, headers, body = self.request("GET", "/api/chats")
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["count"], 2)
        self.assertEqual([chat["name"] for chat in result["chats"]], ["Alpha", "Beta"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.service.list_calls, [(100, None)])

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
            [(100, None), (100, "offset:100"), (100, "offset:200")],
        )

    def test_single_chat_download_applies_local_date_and_latest_message_limit(self):
        status, headers, body = self.request(
            "POST",
            "/api/download",
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
        self.assertEqual((chat_id, limit, until), ("19:alpha@thread.v2", 1, None))
        self.assertEqual(since.date().isoformat(), "2026-09-28")
        self.assertIsNotNone(since.utcoffset())

    def test_multiple_chats_download_as_zip_with_one_text_per_chat(self):
        status, headers, body = self.request(
            "POST",
            "/api/download",
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

    def test_concatenated_zip_contains_one_file_in_selection_order(self):
        status, _, body = self.request(
            "POST",
            "/api/download",
            {
                "chat_ids": ["19:beta@thread.v2", "19:alpha@thread.v2"],
                "concatenate": True,
            },
        )
        self.assertEqual(status, 200)
        with zipfile.ZipFile(BytesIO(body)) as archive:
            self.assertEqual(archive.namelist(), ["conversations.txt"])
            text = archive.read("conversations.txt").decode("utf-8")
        self.assertLess(text.index("Conversation: Beta"), text.index("Conversation: Alpha"))

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

        status, _, body = self.request(
            "POST",
            "/api/download",
            {"chat_ids": ["19:alpha@thread.v2"], "message_limit": "0"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
        self.assertEqual(self.service.connector.calls, [])


if __name__ == "__main__":
    unittest.main()
