import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from teams_exporter.teams_connector import ChatSummary, TeamsConnector
from teams_exporter.teams_exporter import TeamsExporter
from teams_exporter.teams_message_formatter import TeamsHtmlParser, TeamsMessageFormatter


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))


class StubConnector(TeamsConnector):
    def __init__(self, responses=()):
        super().__init__()
        self._spaces_access_token = "spaces-test-token"
        self._spaces_claims = {"oid": "self-guid"}
        self._csa_access_token = "csa-test-token"
        self._skype_token = "skype-test-token"
        self._authz_response = fixture("authz.json")
        self._chat_service_url = "https://chat.example.test"
        self._middle_tier_url = "https://middle.example.test"
        self.responses = list(responses)
        self.requests = []

    def _http_json(self, url, *, method="GET", body=None, headers=None):
        self.requests.append((url, method, body, dict(headers or {})))
        if self.responses:
            return self.responses.pop(0)
        return {}


class TeamsConnectorTests(unittest.TestCase):
    def test_csa_catalog_sorts_by_activity_and_stabilizes_ties(self):
        client = StubConnector([
            fixture("csa_startup.json"),
            fixture("profile_response.json"),
        ])

        chats = client.list_chats()

        self.assertEqual([chat.id for chat in chats], [
            "19:zeta@thread.v2",
            "19:beta@thread.v2",
            "19:alpha@thread.v2",
            "19:untitled@thread.v2",
        ])
        self.assertEqual(chats[0].display_name, "Zeta discussion")
        self.assertTrue(chats[0].unread)
        self.assertEqual(chats[0].chat_type, "group")
        self.assertEqual(chats[0].last_message_preview, "Newest chat item")
        self.assertEqual(chats[2].unread, False)
        self.assertEqual(chats[3].display_name, "Resolved One-to-one Person")
        self.assertEqual(client.requests[1][1], "POST")
        self.assertEqual(
            client.requests[1][3]["Authorization"], "Bearer spaces-test-token"
        )
        self.assertEqual(
            client.requests[0][0],
            TeamsConnector.CSA_STARTUP_URL,
        )
        headers = client.requests[0][3]
        self.assertEqual(headers["Authorization"], "Bearer csa-test-token")
        self.assertEqual(headers["x-skypetoken"], "skype-test-token")
        self.assertNotIn("Authentication", headers)

    def test_partial_csa_catalog_is_not_returned_as_complete(self):
        for metadata in ({"isPartialData": True}, {"hasMoreChats": True}):
            with self.subTest(metadata=metadata):
                client = StubConnector([{"chats": [], "metadata": metadata}])
                with self.assertRaisesRegex(RuntimeError, "partial chat catalog"):
                    client.list_chats()

    def test_service_helpers_send_only_their_service_credentials(self):
        client = StubConnector()
        client._request_chat_service("https://chat.example.test/v1/test")
        client._request_middle_tier("https://middle.example.test/v1/test")
        client._request_csa(TeamsConnector.CSA_STARTUP_URL)
        client._request_teams_authz()

        chat_headers = client.requests[0][3]
        middle_headers = client.requests[1][3]
        csa_headers = client.requests[2][3]
        authz_headers = client.requests[3][3]
        self.assertEqual(chat_headers["Authentication"], "skypetoken=skype-test-token")
        self.assertNotIn("Authorization", chat_headers)
        self.assertEqual(middle_headers["Authorization"], "Bearer spaces-test-token")
        self.assertNotIn("x-skypetoken", middle_headers)
        self.assertEqual(csa_headers["Authorization"], "Bearer csa-test-token")
        self.assertEqual(csa_headers["x-skypetoken"], "skype-test-token")
        self.assertEqual(authz_headers["Authorization"], "Bearer spaces-test-token")
        self.assertEqual(client.requests[3][1], "POST")

    def test_message_pagination_follows_opaque_link_and_sorts_oldest_first(self):
        client = StubConnector([
            fixture("messages_page_1.json"),
            fixture("messages_page_2.json"),
        ])
        conversation_id = "19:fixture@thread.v2"

        messages = client.get_messages(conversation_id)

        self.assertEqual([message["id"] for message in messages], [
            "message-1", "message-2", "message-3"
        ])
        self.assertEqual(len(client.requests), 2)
        initial = client.requests[0][0]
        self.assertIn("/19%3Afixture%40thread.v2/messages", initial)
        self.assertEqual(parse_qs(urlsplit(initial).query), {
            "startTime": ["0"],
            "pageSize": ["100"],
            "view": ["msnp24Equivalent"],
        })
        self.assertEqual(
            client.requests[1][0],
            fixture("messages_page_1.json")["_metadata"]["backwardLink"],
        )

    def test_count_and_inclusive_date_filter_apply_together(self):
        client = StubConnector([
            fixture("messages_page_1.json"),
            fixture("messages_page_2.json"),
        ])
        cutoff = datetime(2026, 9, 28, 10, 1, tzinfo=timezone.utc)

        messages = client.get_messages(
            "19:fixture@thread.v2", limit=2, since=cutoff
        )

        self.assertEqual([message["id"] for message in messages], [
            "message-2", "message-3"
        ])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(
            parse_qs(urlsplit(client.requests[0][0]).query)["pageSize"], ["2"]
        )

    def test_date_filter_does_not_stop_on_an_anomalously_old_message(self):
        old = {
            "id": "system-old",
            "sequenceId": 5,
            "originalarrivaltime": "2026-09-01T00:00:00Z",
            "content": "old system message",
        }
        new = {
            "id": "newer",
            "sequenceId": 4,
            "originalarrivaltime": "2026-09-28T10:02:00Z",
            "content": "in range",
        }
        page_one = {
            "messages": [old, new],
            "_metadata": {
                "backwardLink": "https://chat.example.test/v1/older?cursor=one"
            },
        }
        page_two = {
            "messages": [{
                "id": "also-in-range",
                "sequenceId": 3,
                "originalarrivaltime": "2026-09-28T10:01:00Z",
                "content": "also in range",
            }],
            "_metadata": {},
        }
        client = StubConnector([page_one, page_two])

        messages = client.get_messages(
            "19:fixture@thread.v2",
            since=datetime(2026, 9, 28, 10, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [message["id"] for message in messages],
            ["also-in-range", "newer"],
        )
        self.assertEqual(len(client.requests), 2)

    def test_naive_date_is_rejected(self):
        client = StubConnector()
        with self.assertRaisesRegex(ValueError, "timezone"):
            client.get_messages(
                "19:fixture@thread.v2", since=datetime(2026, 9, 1)
            )
        self.assertEqual(client.requests, [])

    def test_repeated_continuation_link_is_rejected(self):
        link = "https://chat.example.test/v1/older?cursor=cycle"
        client = StubConnector([
            {"messages": [], "_metadata": {"backwardLink": link}},
            {"messages": [], "_metadata": {"backwardLink": link}},
        ])
        with self.assertRaisesRegex(RuntimeError, "repeated a message continuation"):
            client.get_messages("19:fixture@thread.v2")
        self.assertEqual(len(client.requests), 2)

    def test_limit_only_stops_after_enough_messages(self):
        client = StubConnector([fixture("messages_page_1.json")])
        messages = client.get_messages("19:fixture@thread.v2", limit=1)
        self.assertEqual([message["id"] for message in messages], ["message-3"])
        self.assertEqual(len(client.requests), 1)

    def test_connection_metadata_never_contains_token_values(self):
        client = StubConnector()
        client._spaces_claims = {"aud": "api.spaces.skype.com", "exp": 1}
        client._auth_sources = {"spaces": "cache", "csa": "device_code"}
        metadata = client.get_connection_metadata()
        self.assertNotIn("spaces-test-token", repr(metadata))
        self.assertNotIn("csa-test-token", repr(metadata))
        self.assertNotIn("skype-test-token", repr(metadata))


class TeamsMessageFormatterTests(unittest.TestCase):
    def setUp(self):
        self.formatter = TeamsMessageFormatter()

    def test_detects_rich_html_from_message_type_even_when_content_type_is_text(self):
        text = self.formatter.format_message({
            "messagetype": "RichText/Html",
            "contenttype": "text",
            "content": "<p>Hello <strong>world</strong></p>",
        })
        self.assertEqual(text, "Hello **world**")

    def test_semantic_mention_markup_is_delimited_without_duplicate_at(self):
        for display_name in ("Rhode, Lucas", "@Rhode, Lucas"):
            with self.subTest(display_name=display_name):
                text = self.formatter.format_message({
                    "messagetype": "RichText/Html",
                    "content": (
                        "<p><span itemtype='http://schema.skype.com/Mention' "
                        "itemid='8:orgid:example'>"
                        f"{display_name}</span>, Could you respond?</p>"
                    ),
                    "properties": {
                        "mentions": json.dumps([{
                            "id": "8:orgid:example",
                            "displayName": "Rhode, Lucas",
                        }])
                    },
                })
                self.assertEqual(text, "@(Rhode, Lucas), Could you respond?")

    def test_lists_links_blockquotes_and_code_are_readable(self):
        text = self.formatter.format_message({
            "messageType": "RichText/Html",
            "contentType": "text",
            "content": (
                "<p>See <a href='https://example.test'>docs</a></p>"
                "<ul><li>first</li><li>second</li></ul>"
                "<blockquote>quoted</blockquote>"
                "<pre><code>value = 1</code></pre>"
            ),
        })
        self.assertIn("docs (https://example.test)", text)
        self.assertIn("- first", text)
        self.assertIn("- second", text)
        self.assertIn("> quoted", text)
        self.assertIn("value = 1", text)
        self.assertIn("```", text)

    def test_attachment_and_ams_image_markers_are_preserved(self):
        text = self.formatter.format_message({
            "messageType": "RichText/Html",
            "content": (
                "<p>Review</p>"
                "<div itemtype='http://schema.skype.com/AMSImage'>"
                "<img src='https://images.example.test/one.png'></div>"
            ),
            "properties": {
                "files": json.dumps([{
                    "fileName": "report.xlsx",
                    "fileInfo": {"shareUrl": "https://files.example.test/report"},
                }])
            },
        })
        self.assertIn("[Attachment: report.xlsx]", text)
        self.assertIn("https://files.example.test/report", text)
        self.assertIn("[Image: https://images.example.test/one.png]", text)

    def test_deleted_message_gets_a_system_marker(self):
        self.assertEqual(
            self.formatter.format_message({"messagetype": "MessageDelete"}),
            "[System message: MessageDelete]",
        )


class TeamsExporterTests(unittest.TestCase):
    def test_export_writes_header_authors_timestamps_and_formatted_body(self):
        class FakeConnector:
            def __init__(self):
                self.calls = []

            def get_messages(self, conversation_id, *, limit=None, since=None):
                self.calls.append((conversation_id, limit, since))
                return [{
                    "id": "one",
                    "sequenceId": 1,
                    "originalarrivaltime": "2026-09-28T10:00:00Z",
                    "imdisplayname": "Taylor",
                    "messagetype": "RichText/Html",
                    "content": "<p>Hi <b>there</b></p>",
                }]

        connector = FakeConnector()
        exporter = TeamsExporter(connector, TeamsMessageFormatter())
        chat = ChatSummary("19:fake", "Sample chat")
        destination = Path(self.id().replace(".", "_") + ".txt")
        try:
            output = exporter.export_chat(chat, destination, limit=20)
            content = output.read_text(encoding="utf-8-sig")
            self.assertIn("Conversation: Sample chat", content)
            self.assertIn("Conversation ID: 19:fake", content)
            self.assertIn("[2026-09-28T10:00:00Z] Taylor", content)
            self.assertIn("Hi **there**", content)
            self.assertEqual(connector.calls[0][0:2], ("19:fake", 20))
        finally:
            destination.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()


