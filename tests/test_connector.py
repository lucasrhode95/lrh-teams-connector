import base64
import json
import tempfile
import threading
import urllib.error
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import msal

from teams_exporter.teams_connector import (
    ChatSummary,
    ExtractionCancelled,
    TeamsConnector,
)
from teams_exporter.teams_exporter import TeamsExporter
from teams_exporter.teams_message_formatter import TeamsHtmlParser, TeamsMessageFormatter
from teams_exporter.token_provider import AuthenticationRequiredError, TokenProvider


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))


class FakeEncryptedPersistence:
    """Test-only encrypted-at-rest stand-in for the OS credential provider."""

    is_encrypted = True
    _prefix = b"test-encrypted:"

    def __init__(self, location):
        self.location = Path(location)

    def get_location(self):
        return str(self.location)

    def time_last_modified(self):
        return self.location.stat().st_mtime if self.location.exists() else 0

    def load(self):
        contents = self.location.read_bytes()
        if not contents.startswith(self._prefix):
            raise ValueError("not a test encrypted cache")
        return base64.b64decode(contents[len(self._prefix) :]).decode("utf-8")

    def save(self, contents):
        self.location.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self._prefix + base64.b64encode(contents.encode("utf-8"))
        self.location.write_bytes(encrypted)


def sanitized_msal_cache_state(username="fixture@example.test"):
    cache = msal.SerializableTokenCache()
    cache.deserialize(
        json.dumps(
            {
                "Account": {
                    "fixture-account": {
                        "home_account_id": "fixture-home",
                        "environment": "login.microsoftonline.com",
                        "realm": "fixture-tenant",
                        "local_account_id": "fixture-user",
                        "username": username,
                    }
                },
                "AccessToken": {},
                "AppMetadata": {},
                "IdToken": {},
                "RefreshToken": {},
            }
        )
    )
    return cache.serialize()


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

    def _http_json(
        self,
        url,
        method="GET",
        body=None,
        headers=None,
        cancel_event=None,
    ):
        self.requests.append((url, method, body, dict(headers or {})))
        self.last_request_retry_count = 0
        if self.responses:
            return self.responses.pop(0)
        return {}


class TeamsConnectorTests(unittest.TestCase):
    def test_cached_account_metadata_reads_identity_without_acquiring_tokens(self):
        client = TeamsConnector()
        cached_account = {
            "username": "taylor@example.test",
            "local_account_id": "user-guid",
            "tenant_id": "tenant-guid",
            "home_account_id": "user-guid.tenant-guid",
        }
        get_cached_account = Mock(return_value=cached_account)
        with patch.object(client.token_provider, "get_cached_account", get_cached_account):
            metadata = client.get_cached_account_metadata()

        self.assertEqual(metadata["username"], "taylor@example.test")
        self.assertEqual(metadata["user_id"], "user-guid")
        get_cached_account.assert_called_once_with()

    def test_profile_picture_uri_uses_teams_web_route_without_fetching_an_image(self):
        client = StubConnector()
        client._spaces_claims["name"] = "Taylor Example"

        uri = client.get_profile_picture_uri()

        self.assertIsNotNone(uri)
        parts = urlsplit(uri)
        self.assertEqual(parts.netloc, "middle.example.test")
        self.assertEqual(
            parts.path,
            "/beta/users/self-guid/profilepicturev2/8:orgid:self-guid",
        )
        self.assertEqual(
            parse_qs(parts.query),
            {"displayname": ["Taylor Example"], "size": ["HR64x64"]},
        )
        self.assertEqual(client.requests, [])

    def test_profile_picture_uses_authenticated_middle_tier_request(self):
        client = StubConnector()
        uri = client.get_profile_picture_uri()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {
            "Content-Type": "image/png; charset=binary",
            "Content-Length": "4",
        }
        response.read.return_value = b"PNG!"
        opener = Mock()
        opener.open.return_value = response

        with patch("urllib.request.build_opener", return_value=opener):
            image, content_type = client.get_profile_picture(uri)

        self.assertEqual(image, b"PNG!")
        self.assertEqual(content_type, "image/png")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer spaces-test-token")
        self.assertIn("image/", request.get_header("Accept"))

    def test_profile_picture_fetch_rejects_urls_outside_middle_tier(self):
        client = StubConnector()

        with self.assertRaisesRegex(RuntimeError, "outside the configured Middle Tier"):
            client.get_profile_picture("https://images.example.test/profilepicturev2/user")

        self.assertFalse(
            client.can_fetch_profile_picture(
                "https://middle.example.test/beta/users/me/profilepicturev2/../../messages"
            )
        )

    def test_chat_rows_use_backend_avatar_urls_or_one_to_one_picture_uri(self):
        client = StubConnector([{
            "chats": [
                {
                    "id": "19:group@thread.v2",
                    "title": "Group",
                    "chatType": "group",
                    "avatarUrl": "https://images.example.test/group.png",
                    "lastMessage": {},
                },
                {
                    "id": "19:direct@thread.v2",
                    "title": "Taylor Example",
                    "chatType": "chat",
                    "isOneOnOne": True,
                    "members": [
                        {"objectId": "self-guid", "mri": "8:orgid:self-guid"},
                        {
                            "objectId": "peer-guid",
                            "mri": "8:orgid:peer-guid",
                            "displayName": "Jordan Example",
                        },
                    ],
                    "lastMessage": {},
                },
            ],
            "metadata": {"isPartialData": False, "hasMoreChats": False},
        }])

        chats = {chat.id: chat for chat in client.list_chats()}

        self.assertEqual(
            chats["19:group@thread.v2"].avatar_url,
            "https://images.example.test/group.png",
        )
        direct_picture = urlsplit(chats["19:direct@thread.v2"].avatar_url)
        self.assertEqual(
            direct_picture.path,
            "/beta/users/self-guid/profilepicturev2/8:orgid:peer-guid",
        )
        self.assertEqual(
            parse_qs(direct_picture.query)["displayname"], ["Jordan Example"]
        )
        self.assertEqual(len(client.requests), 1)

    def test_send_message_posts_escaped_plain_text_with_chat_service_credentials(self):
        client = StubConnector(
            [{"id": "sent-message-id", "OriginalArrivalTime": 1790123456789}]
        )
        client._spaces_claims["name"] = "Taylor Example"

        result = client.send_message(
            "19:chat@thread.v2",
            "Hello <Teams> & friends\nsecond line",
        )

        url, method, body, headers = client.requests[0]
        self.assertEqual(
            url,
            "https://chat.example.test/v1/users/ME/conversations/"
            "19%3Achat%40thread.v2/messages",
        )
        self.assertEqual(method, "POST")
        self.assertEqual(
            body["content"],
            "<p>Hello &lt;Teams&gt; &amp; friends<br>second line</p>",
        )
        self.assertEqual(body["messagetype"], "RichText/Html")
        self.assertEqual(body["contenttype"], "text")
        self.assertTrue(body["clientmessageid"].isdigit())
        self.assertEqual(body["imdisplayname"], "Taylor Example")
        self.assertEqual(body["properties"], {"importance": "", "subject": None})
        self.assertEqual(headers["Authentication"], "skypetoken=skype-test-token")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(result["message_id"], "sent-message-id")
        self.assertEqual(result["client_message_id"], body["clientmessageid"])
        self.assertEqual(result["sent_at"], "2026-09-23T00:30:56.789Z")

    def test_send_message_rejects_empty_or_oversized_text_without_request(self):
        client = StubConnector()

        for text in ("  ", "x" * 10_001, "bad\x00text"):
            with self.subTest(length=len(text)):
                with self.assertRaises(ValueError):
                    client.send_message("19:chat@thread.v2", text)

        self.assertEqual(client.requests, [])

    def test_http_client_accepts_an_empty_successful_response(self):
        client = TeamsConnector()
        with patch("urllib.request.urlopen") as open_url:
            response = open_url.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b""

            result = client._http_json(
                "https://chat.example.test/send",
                method="POST",
                body={"content": "sanitized test"},
            )

        self.assertEqual(result, {})

    def test_members_enrich_blank_display_names_from_middle_tier_profiles(self):
        client = StubConnector(
            [{"members": [{"id": "8:orgid:ABC123", "displayName": ""}]}]
        )
        with patch.object(
            client,
            "_fetch_middle_tier_profile_names",
            return_value={"abc123": "Taylor Example"},
        ) as resolve_names:
            members = client.get_conversation_members("19:alpha@thread.v2")

        self.assertEqual(members[0]["displayName"], "Taylor Example")
        resolve_names.assert_called_once_with(["abc123"])

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
            "48:notes",
        ])
        self.assertEqual(chats[-1].id, "48:notes")
        self.assertTrue(chats[-1].is_self_chat)
        self.assertEqual(chats[-1].display_name, "Notes to self")
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

    def test_csa_generic_chat_type_uses_one_to_one_flag_and_complete_roster(self):
        def roster(size):
            return [
                {"mri": f"8:orgid:{identifier}", "objectId": identifier}
                for identifier in ["self-guid"]
                + [f"participant-{index}" for index in range(size - 1)]
            ]

        rows = [
            {
                "id": "19:group-a@thread.v2",
                "title": "Group A",
                "chatType": "chat",
                "isOneOnOne": False,
                "rosterVersion": 1,
                "members": roster(9),
                "lastMessage": {"originalArrivalTime": "2026-09-30T10:00:00Z"},
            },
            {
                "id": "19:group-b@thread.v2",
                "title": "Group B",
                "chatType": "chat",
                "isOneOnOne": False,
                "rosterVersion": 1,
                "members": roster(4),
                "lastMessage": {"originalArrivalTime": "2026-09-30T09:00:00Z"},
            },
            {
                "id": "19:group-c@thread.v2",
                "title": "Group C",
                "chatType": "chat",
                "isOneOnOne": False,
                "rosterVersion": 1,
                "members": roster(5),
                "lastMessage": {"originalArrivalTime": "2026-09-30T08:00:00Z"},
            },
            {
                "id": "19:person@thread.v2",
                "title": "Person",
                "chatType": "chat",
                "isOneOnOne": True,
                "rosterVersion": 1,
                "members": roster(2),
                "lastMessage": {"originalArrivalTime": "2026-09-30T07:00:00Z"},
            },
            {
                "id": "19:contradictory@thread.v2",
                "title": "Contradictory roster",
                "chatType": "chat",
                "isOneOnOne": True,
                "rosterVersion": 1,
                "members": roster(3),
                "lastMessage": {"originalArrivalTime": "2026-09-30T06:00:00Z"},
            },
            {
                "id": "19:unknown-many@thread.v2",
                "title": "Unknown flag with complete roster",
                "chatType": "chat",
                "rosterVersion": 1,
                "members": roster(4),
                "lastMessage": {"originalArrivalTime": "2026-09-30T05:00:00Z"},
            },
            {
                "id": "19:unknown-incomplete@thread.v2",
                "title": "Unknown flag with incomplete roster metadata",
                "chatType": "chat",
                "members": roster(4),
                "lastMessage": {"originalArrivalTime": "2026-09-30T04:00:00Z"},
            },
        ]
        expected = {
            "19:group-a@thread.v2": "group",
            "19:group-b@thread.v2": "group",
            "19:group-c@thread.v2": "group",
            "19:person@thread.v2": "oneOnOne",
            "19:contradictory@thread.v2": "group",
            "19:unknown-many@thread.v2": "group",
            "19:unknown-incomplete@thread.v2": "chat",
        }

        startup = StubConnector([{
            "chats": rows,
            "metadata": {"isPartialData": False, "hasMoreChats": False},
        }])
        startup_chats = {chat.id: chat for chat in startup.list_chats()}
        self.assertEqual(
            {conversation_id: startup_chats[conversation_id].chat_type for conversation_id in expected},
            expected,
        )

        updates = StubConnector([{
            "chats": rows,
            "channels": [],
            "metadata": {"syncToken": "sanitized-next-token", "isPartialData": False, "hasMoreChats": False},
        }])
        update_chats = updates.get_chat_updates()["chats"]
        self.assertEqual(
            {chat.id: chat.chat_type for chat in update_chats if chat.id in expected},
            expected,
        )

    def test_csa_updates_send_sync_token_and_include_channel_rows(self):
        client = StubConnector([{
            "chats": [{
                "id": "19:alpha@thread.v2",
                "title": "Alpha",
                "chatType": "group",
                "lastMessage": {"content": "chat update", "originalArrivalTime": "2026-09-28T12:00:00Z"},
            }],
            "channels": [{
                "conversationId": "19:channel@thread.tacv2",
                "title": "General",
                "teamName": "Analytics",
                "lastMessage": {"content": "channel update", "originalArrivalTime": "2026-09-28T13:00:00Z"},
                "avatarUrl": "https://middle.example.test/beta/users/self-guid/profilepicturev2/8:orgid:team-guid",
            }, {
                "id": "19:deleted@thread.tacv2",
                "displayName": "Deleted channel",
                "isDeleted": True,
            }],
            "metadata": {
                "syncToken": "sanitized-next-sync-token",
                "isPartialData": False,
                "hasMoreChats": False,
            },
        }])

        result = client.get_chat_updates("sanitized-previous-sync-token")

        self.assertEqual(client.requests[0][0], TeamsConnector.CSA_UPDATES_URL)
        self.assertEqual(
            client.requests[0][3]["x-ms-synctoken"],
            "sanitized-previous-sync-token",
        )
        self.assertEqual(result["sync_token"], "sanitized-next-sync-token")
        self.assertEqual(result["deleted_ids"], ["19:deleted@thread.tacv2"])
        channel = next(chat for chat in result["chats"] if chat.chat_type == "channel")
        self.assertEqual(channel.id, "19:channel@thread.tacv2")
        self.assertEqual(channel.display_name, "Analytics · General")
        self.assertIn("profilepicturev2", channel.avatar_url)
        self.assertEqual(result["chats"][0].id, channel.id)
        self.assertNotIn("19:deleted@thread.tacv2", {chat.id for chat in result["chats"]})

    def test_expired_csa_sync_token_restarts_from_a_fresh_snapshot(self):
        client = StubConnector([
            {"error": {"code": "SyncTokenExpired"}},
            {
                "chats": [],
                "channels": [],
                "metadata": {"syncToken": "sanitized-recovered-token", "isPartialData": False, "hasMoreChats": False},
            },
        ])

        result = client.get_chat_updates("sanitized-expired-token")

        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[0][3]["x-ms-synctoken"], "sanitized-expired-token")
        self.assertNotIn("x-ms-synctoken", client.requests[1][3])
        self.assertTrue(result["is_bootstrap"])
        self.assertEqual(result["sync_token"], "sanitized-recovered-token")

    def test_partial_csa_catalog_follows_chat_service_pages_and_preserves_csa_rows(self):
        next_page = "https://chat.example.test/v1/users/ME/conversations?cursor=next"
        client = StubConnector([
            {
                "chats": [{
                    "id": "19:rich@thread.v2",
                    "title": "Rich CSA chat",
                    "lastMessage": {
                        "content": "CSA preview",
                        "originalArrivalTime": "2026-09-28T10:00:00Z",
                    },
                    "isRead": False,
                }],
                "metadata": {"hasMoreChats": True},
            },
            {
                "conversations": [
                    {
                        "id": "19:rich@thread.v2",
                        "threadProperties": {"topic": "Raw duplicate title"},
                    },
                    {
                        "id": "19:extra@thread.v2",
                        "threadProperties": {
                            "topic": "Extra chat",
                            "productThreadType": "Chat",
                            "threadType": "chat",
                        },
                        "properties": {"lastimreceivedtime": 1790590000000},
                    },
                    {
                        "id": "48:notifications",
                        "threadProperties": {
                            "productThreadType": "StreamOfNotifications",
                            "threadType": "streamofnotifications",
                        },
                    },
                    {
                        "id": "19:channel@thread.tacv2",
                        "threadProperties": {"productThreadType": "TeamsStandardChannel"},
                    },
                    {
                        "id": "19:future@thread.v2",
                        "threadProperties": {"productThreadType": "FutureRecord"},
                    },
                ],
                "_metadata": {"backwardLink": next_page},
            },
            {
                "conversations": [
                    {"id": "19:extra@thread.v2"},
                    {
                        "id": "19:last@thread.v2",
                        "threadProperties": {
                            "topic": "Last meeting",
                            "productThreadType": "Meeting",
                            "threadType": "meeting",
                        },
                    },
                ],
                "_metadata": {},
            },
        ])

        chats = client.list_chats()

        chats_by_id = {chat.id: chat for chat in chats}
        self.assertEqual(set(chats_by_id), {
            "48:notes",
            "19:rich@thread.v2",
            "19:extra@thread.v2",
            "19:last@thread.v2",
        })
        self.assertEqual(chats_by_id["19:rich@thread.v2"].display_name, "Rich CSA chat")
        self.assertEqual(
            chats_by_id["19:rich@thread.v2"].last_message_preview, "CSA preview"
        )
        self.assertTrue(chats_by_id["19:rich@thread.v2"].unread)
        self.assertEqual(chats_by_id["19:extra@thread.v2"].display_name, "Extra chat")
        self.assertEqual(chats_by_id["19:last@thread.v2"].display_name, "Last meeting")
        self.assertTrue(client.catalog_coverage.catalog_complete is False)
        self.assertTrue(client.catalog_coverage.csa_reported_partial)
        self.assertEqual(client.catalog_coverage.chat_service_unique_rows, 6)
        self.assertEqual(client.catalog_coverage.fallback_chats_added, 2)
        self.assertEqual(client.catalog_coverage.non_chat_service_rows_excluded, 2)
        self.assertEqual(client.catalog_coverage.unclassified_service_rows_excluded, 1)
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.requests[0][0], TeamsConnector.CSA_STARTUP_URL)
        first_catalog_url = client.requests[1][0]
        self.assertEqual(
            parse_qs(urlsplit(first_catalog_url).query),
            {"view": ["mychats"], "pageSize": ["500"]},
        )
        self.assertEqual(client.requests[2][0], next_page)

    def test_fallback_one_to_one_name_uses_read_only_roster(self):
        client = StubConnector([
            {
                "chats": [],
                "metadata": {"isPartialData": True, "hasMoreChats": False},
            },
            {
                "conversations": [{
                    "id": "19:untitled@thread.v2",
                    "threadProperties": {
                        "productThreadType": "OneToOneChat",
                        "threadType": "chat",
                    },
                }],
                "_metadata": {},
            },
            {
                "members": [
                    {"id": "8:orgid:self-guid", "displayName": "Taylor Example"},
                    {"id": "8:orgid:peer-guid", "displayName": "Jordan Example"},
                ]
            },
        ])

        chats = client.list_chats()

        chat = next(item for item in chats if item.id == "19:untitled@thread.v2")
        self.assertEqual(chat.display_name, "Jordan Example")
        self.assertEqual(client.catalog_coverage.fallback_name_lookups_attempted, 1)
        self.assertEqual(client.catalog_coverage.fallback_chats_without_resolved_name, 0)
        self.assertTrue(client.catalog_coverage.catalog_complete)

    def test_unspecified_csa_completeness_uses_paginated_fallback(self):
        client = StubConnector([
            {"chats": [], "metadata": {}},
            {"conversations": [], "_metadata": {}},
        ])

        chats = client.list_chats()

        self.assertEqual([chat.id for chat in chats], ["48:notes"])
        self.assertIsNone(client.catalog_coverage.csa_reported_partial)
        self.assertEqual(client.catalog_coverage.chat_service_unique_rows, 0)
        self.assertTrue(client.catalog_coverage.catalog_complete)
        self.assertEqual(len(client.requests), 2)

    def test_raw_self_chat_is_named_and_counted_as_a_chat(self):
        client = StubConnector([
            {"chats": [], "metadata": {"isPartialData": True}},
            {"conversations": [{
                "id": "48:notes",
                "threadProperties": {"productThreadType": "StreamOfNotes"},
            }], "_metadata": {}},
        ])

        chats = client.list_chats()

        self_chat = next(chat for chat in chats if chat.is_self_chat)
        self.assertEqual(self_chat.display_name, "Notes to self")
        self.assertEqual(self_chat.chat_type, "chat")
        self.assertEqual(client.catalog_coverage.fallback_chats_added, 1)
        self.assertEqual(client.catalog_coverage.fallback_chats_without_resolved_name, 0)

    def test_catalog_rejects_csa_rows_without_ids_instead_of_silently_dropping_them(self):
        client = StubConnector([
            {"chats": [{"title": "Missing identifier"}], "metadata": {
                "isPartialData": False,
                "hasMoreChats": False,
            }},
        ])

        with self.assertRaisesRegex(RuntimeError, "chat row has no ID"):
            client.list_chats()

    def test_catalog_rejects_chat_service_rows_without_ids(self):
        client = StubConnector([
            {"chats": [], "metadata": {"isPartialData": True}},
            {"conversations": [{"threadProperties": {
                "productThreadType": "Chat",
            }}], "_metadata": {}},
        ])

        with self.assertRaisesRegex(RuntimeError, "catalog row without an ID"):
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
        self.assertEqual(
            parse_qs(urlsplit(client.requests[0][0]).query)["startTime"],
            [str(TeamsConnector._epoch_milliseconds(cutoff))],
        )
        self.assertNotIn("endTime", parse_qs(urlsplit(client.requests[0][0]).query))

    def test_inclusive_date_range_works_with_unlimited_message_count(self):
        client = StubConnector([
            fixture("messages_page_1.json"),
            fixture("messages_page_2.json"),
        ])
        since = datetime(2026, 9, 28, 10, 1, tzinfo=timezone.utc)
        until = datetime(2026, 9, 28, 10, 2, tzinfo=timezone.utc)

        messages = client.get_messages(
            "19:fixture@thread.v2",
            limit=None,
            since=since,
            until=until,
        )

        self.assertEqual(
            [message["id"] for message in messages],
            ["message-2", "message-3"],
        )
        self.assertEqual(len(client.requests), 2)

    def test_until_must_be_timezone_aware_and_not_before_since(self):
        client = StubConnector()
        with self.assertRaisesRegex(ValueError, "until must include a timezone"):
            client.get_messages(
                "19:fixture@thread.v2",
                until=datetime(2026, 9, 1),
            )
        with self.assertRaisesRegex(ValueError, "earlier than or equal"):
            client.get_messages(
                "19:fixture@thread.v2",
                since=datetime(2026, 9, 2, tzinfo=timezone.utc),
                until=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
        self.assertEqual(client.requests, [])

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
        self.assertTrue(client.last_message_read_coverage["limit_reached"])
        self.assertFalse(client.last_message_read_coverage["complete"])

    def test_read_requests_retry_transient_http_failures_and_count_retries(self):
        client = TeamsConnector()
        unavailable = urllib.error.HTTPError(
            "https://chat.example.test/messages",
            503,
            "Service Unavailable",
            {"Retry-After": "0"},
            None,
        )
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.return_value = b'{"ok": true}'
        with patch("urllib.request.urlopen", side_effect=[unavailable, response]) as urlopen:
            with patch("time.sleep"):
                payload = client._http_json("https://chat.example.test/messages")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(client.last_request_retry_count, 1)

    def test_read_retry_budget_is_four_total_attempts(self):
        client = TeamsConnector()
        failures = [
            urllib.error.HTTPError(
                "https://chat.example.test/messages", 503, "Unavailable", {}, None
            )
            for _ in range(client.READ_REQUEST_MAX_ATTEMPTS)
        ]
        with patch("urllib.request.urlopen", side_effect=failures) as urlopen:
            with patch("time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "4-attempt limit"):
                    client._http_json("https://chat.example.test/messages")

        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(client.last_request_retry_count, 3)

    def test_unknown_read_error_is_retried_with_bounded_policy(self):
        client = TeamsConnector()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.return_value = b'{"ok": true}'
        with patch(
            "urllib.request.urlopen",
            side_effect=[RuntimeError("sanitized transient fixture"), response],
        ) as urlopen:
            with patch("time.sleep") as sleep:
                payload = client._http_json("https://chat.example.test/messages")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(client.last_request_retry_count, 1)
        sleep.assert_called_once()

    def test_failed_page_reports_retries_without_discarding_prior_progress_count(self):
        client = StubConnector()
        progress = []

        def fail_page(_url, *, cancel_event=None):
            client.last_request_retry_count = 3
            raise RuntimeError("sanitized exhausted request")

        client._request_chat_service = fail_page
        with self.assertRaisesRegex(RuntimeError, "exhausted"):
            list(
                client.iter_message_pages(
                    "19:fixture@thread.v2",
                    progress_callback=lambda pages, messages, retries: progress.append(
                        (pages, messages, retries)
                    ),
                )
            )

        self.assertEqual(progress, [(0, 0, 3)])
        self.assertEqual(client.last_message_read_coverage["retry_count"], 3)

    def test_write_requests_are_not_retried(self):
        client = TeamsConnector()
        failure = urllib.error.HTTPError(
            "https://chat.example.test/send", 503, "Unavailable", {}, None
        )
        with patch("urllib.request.urlopen", side_effect=failure) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                client._http_json(
                    "https://chat.example.test/send", method="POST", body=b"{}"
                )

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(client.last_request_retry_count, 0)

    def test_retry_after_and_exponential_delays_are_bounded(self):
        self.assertEqual(TeamsConnector._read_retry_delay(1, "1000"), 10.0)
        self.assertEqual(TeamsConnector._read_retry_delay(2, "invalid"), 0.5)

    def test_cancellation_interrupts_retry_backoff(self):
        client = TeamsConnector()
        cancel_event = threading.Event()
        failure = urllib.error.HTTPError(
            "https://chat.example.test/messages", 503, "Unavailable", {}, None
        )
        with patch("urllib.request.urlopen", side_effect=failure) as urlopen:
            with patch.object(cancel_event, "wait", return_value=True):
                with self.assertRaisesRegex(ExtractionCancelled, "cancelled"):
                    client._http_json(
                        "https://chat.example.test/messages",
                        cancel_event=cancel_event,
                    )

        self.assertEqual(urlopen.call_count, 1)

    def test_connection_metadata_never_contains_token_values(self):
        client = StubConnector()
        client._spaces_claims = {"aud": "api.spaces.skype.com", "exp": 1}
        client._auth_sources = {"spaces": "cache", "csa": "device_code"}
        metadata = client.get_connection_metadata()
        self.assertNotIn("spaces-test-token", repr(metadata))
        self.assertNotIn("csa-test-token", repr(metadata))
        self.assertNotIn("skype-test-token", repr(metadata))

    def test_default_cache_path_is_absolute_and_independent_of_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            with patch.dict("os.environ", {"LOCALAPPDATA": str(root)}):
                with patch("os.getcwd", return_value=str(elsewhere)):
                    cache_path = TeamsConnector().token_provider.cache_path

        self.assertEqual(cache_path, root / "scrape-teams-chat" / ".teams_token_cache.json")
        self.assertTrue(cache_path.is_absolute())

    def test_injected_cache_path_uses_encrypted_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "test-cache.bin"
            client = TeamsConnector(cache_path=cache_path)
            build = Mock(side_effect=FakeEncryptedPersistence)
            with patch("msal_extensions.build_encrypted_persistence", build):
                client.token_provider.load_cache()

            build.assert_called_once_with(str(cache_path))

    def test_unsecure_mode_uses_separate_plaintext_development_cache(self):
        from msal_extensions import FilePersistence, PersistedTokenCache

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "debug-cache.json"
            provider = TokenProvider(cache_path=cache_path, unsecure=True)
            cache = provider.load_cache()
            sanitized_state = sanitized_msal_cache_state()
            self.assertIsInstance(cache, PersistedTokenCache)
            self.assertIsInstance(cache._persistence, FilePersistence)
            cache._persistence.save(sanitized_state)

            self.assertEqual(cache_path.read_text(encoding="utf-8"), sanitized_state)
            self.assertEqual(
                list(provider.load_cache().search(msal.TokenCache.CredentialType.ACCOUNT))[0]["username"],
                "fixture@example.test",
            )
            self.assertEqual(provider.cache_path, cache_path.resolve())
            self.assertEqual(provider.mode, "plaintext-development")

    def test_unsecure_connector_uses_the_development_cache_path(self):
        client = TeamsConnector(unsecure=True)
        self.assertTrue(client.token_provider.unsecure)
        self.assertEqual(
            client.token_provider.cache_path,
            (Path(__file__).parent.parent / ".local" / "teams-msal-debug-cache.json").resolve(),
        )
        self.assertIn(str(client.token_provider.cache_path), TokenProvider.unsecure_warning(client.token_provider.cache_path))

    def test_login_keeps_the_existing_scopes_and_uses_persisted_msal_cache(self):
        from msal_extensions import PersistedTokenCache

        with tempfile.TemporaryDirectory() as directory:
            client = TeamsConnector(cache_path=Path(directory) / "cache")
            app = Mock()
            acquire = Mock(
                side_effect=[
                    ({"access_token": "fixture-spaces-token"}, "cache"),
                    ({"access_token": "fixture-csa-token"}, "cache"),
                ]
            )
            authz = {
                "tokens": {"skypeToken": "fixture-skype-token"},
                "regionGtms": {
                    "chatService": "https://chat.example.test",
                    "middleTier": "https://middle.example.test",
                },
            }
            with (
                patch("msal_extensions.build_encrypted_persistence", FakeEncryptedPersistence),
                patch("teams_exporter.token_provider.msal.PublicClientApplication", return_value=app) as create_app,
                patch.object(client.token_provider, "_acquire_resource_token", acquire),
                patch.object(client, "_request_teams_authz", return_value=authz),
            ):
                client.login(interactive=False)

            cache = create_app.call_args.kwargs["token_cache"]
            self.assertIsInstance(cache, PersistedTokenCache)
            self.assertEqual(
                [call.args[1] for call in acquire.call_args_list],
                [client.SPACES_SCOPE, client.CSA_SCOPE],
            )
            self.assertEqual(
                [call.kwargs["interactive"] for call in acquire.call_args_list],
                [False, False],
            )
            self.assertEqual(client._auth_sources, {"spaces": "cache", "csa": "cache"})

    def test_missing_encrypted_provider_fails_closed_without_writing_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            client = TeamsConnector(cache_path=cache_path)
            with patch(
                "msal_extensions.build_encrypted_persistence",
                side_effect=RuntimeError("fixture failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "No plaintext cache will be used") as raised:
                    client.token_provider.load_cache()

            self.assertNotIn("fixture failure", str(raised.exception))
            self.assertFalse(cache_path.exists())

    def test_silent_msal_acquisition_renews_cached_expiring_access_token(self):
        client = TeamsConnector(cache_path="unused-cache.json")
        provider = client.token_provider
        account = {"home_account_id": "sanitized-account"}
        app = Mock()
        app.get_accounts.return_value = [account]
        app.acquire_token_silent.return_value = {"access_token": "sanitized-renewed-token"}

        result, source = provider._acquire_resource_token(
            app, client.SPACES_SCOPE, interactive=False
        )

        self.assertEqual(source, "cache")
        self.assertEqual(result["access_token"], "sanitized-renewed-token")
        app.acquire_token_silent.assert_called_once_with(
            scopes=[client.SPACES_SCOPE], account=account
        )

    def test_invalid_refresh_token_fails_fast_with_sign_in_guidance(self):
        client = TeamsConnector(cache_path="unused-cache.json")
        provider = client.token_provider
        app = Mock()
        app.get_accounts.return_value = [{"home_account_id": "sanitized-account"}]
        app.acquire_token_silent.return_value = {"error": "invalid_grant"}

        with self.assertRaisesRegex(AuthenticationRequiredError, "no longer accepts") as raised:
            provider._acquire_resource_token(app, client.SPACES_SCOPE, interactive=False)

        self.assertIn("python -m teams_exporter auth", str(raised.exception))
        app.acquire_token_silent.assert_called_once()

    def test_missing_unsecure_session_points_to_unsecure_sign_in_command(self):
        provider = TokenProvider(cache_path="unused-debug-cache.json", unsecure=True)
        app = Mock()
        app.get_accounts.return_value = []

        with self.assertRaisesRegex(AuthenticationRequiredError, "auth --unsecure"):
            provider._acquire_resource_token(
                app,
                TeamsConnector.SPACES_SCOPE,
                interactive=False,
            )

    def test_unknown_msal_errors_retry_without_claiming_credentials_are_invalid(self):
        client = TeamsConnector(cache_path="unused-cache.json")
        provider = client.token_provider
        app = Mock()
        app.get_accounts.return_value = [{"home_account_id": "sanitized-account"}]
        app.acquire_token_silent.return_value = {"error": "unclassified_fixture_error"}
        with patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "service request failed after 3 attempts") as raised:
                provider._acquire_resource_token(app, client.SPACES_SCOPE, interactive=False)

        self.assertNotIn("credentials are invalid", str(raised.exception).casefold())
        self.assertEqual(app.acquire_token_silent.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_safe_network_detail_keeps_cause_but_redacts_tokens_and_url_queries(self):
        detail = TeamsConnector._safe_exception_detail(
            "connection refused for https://teams.example.test/path?token=secret-value"
        )

        self.assertIn("connection refused", detail)
        self.assertIn("https://teams.example.test/path", detail)
        self.assertNotIn("secret-value", detail)


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

    def test_adjacent_fragments_for_same_mention_share_one_delimiter(self):
        text = self.formatter.format_message({
            "messageType": "RichText/Html",
            "content": (
                "<p><span itemtype='http://schema.skype.com/Mention' "
                "itemid='8:orgid:juniper'>Parker,</span> "
                "<span itemtype='http://schema.skype.com/Mention' "
                "itemid='8:orgid:juniper'>Juniper</span>"
                " Please review the example plan.</p>"
            ),
        })
        self.assertEqual(
            text,
            "@(Parker, Juniper) Please review the example plan.",
        )

    def test_adjacent_different_mentions_keep_separate_delimiters(self):
        text = self.formatter.format_message({
            "messageType": "RichText/Html",
            "content": (
                "<span itemtype='http://schema.skype.com/Mention' "
                "itemid='8:orgid:one'>Parker</span> "
                "<span itemtype='http://schema.skype.com/Mention' "
                "itemid='8:orgid:two'>Juniper</span>"
            ),
        })
        self.assertEqual(text, "@(Parker) @(Juniper)")

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

    def test_swift_uriobject_decodes_adaptive_card_text_without_layout_or_payload(self):
        card = {
            "type": "message/card",
            "summary": "Approval required",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": [{
                        "type": "Container",
                        "spacing": "large",
                        "items": [
                            {"type": "TextBlock", "text": "Approval required", "weight": "bolder"},
                            {"type": "TextBlock", "text": "Please review this request."},
                            {"type": "FactSet", "facts": [
                                {"title": "Case ID", "value": "CASE-2048"},
                                {"title": "Status", "value": "Pending"},
                            ]},
                            {"type": "Image", "url": "https://images.example.test/layout.png", "altText": "Equipment diagram"},
                        ],
                    }],
                    "actions": [{
                        "type": "Action.OpenUrl",
                        "title": "Open case",
                        "url": "https://example.test/cases/CASE-2048",
                    }],
                    "padding": "large",
                },
            }],
            "entities": [{"type": "layout-only", "value": "discard this metadata"}],
        }
        encoded = base64.b64encode(json.dumps(card).encode("utf-8")).decode("ascii")
        content = (
            '<p>Before card</p>'
            '<URIObject type="SWIFT.1" url_thumbnail="https://images.example.test/preview.png">'
            '<a href="https://cards.example.test">Card preview</a>'
            '<Title>Approval required</Title>'
            f'<Swift b64="{encoded}" />'
            '<Description />'
            '</URIObject><p>After card</p>'
        )

        text = self.formatter.format_message({
            "messageType": "RichText/Html",
            "content": content,
        })

        self.assertIn("Before card", text)
        self.assertIn("After card", text)
        self.assertIn("Please review this request.", text)
        self.assertIn("Case ID: CASE-2048", text)
        self.assertIn("Status: Pending", text)
        self.assertIn("Equipment diagram", text)
        self.assertIn("Open case", text)
        self.assertIn("https://example.test/cases/CASE-2048", text)
        self.assertIn("https://cards.example.test", text)
        self.assertEqual(text.count("Approval required"), 1)
        self.assertNotIn(encoded, text)
        self.assertNotIn("discard this metadata", text)
        self.assertNotIn("padding", text)
        self.assertNotIn("weight", text)
        self.assertNotIn("url_thumbnail", text)

    def test_malformed_swift_payload_keeps_visible_uriobject_text_only(self):
        text = self.formatter.format_message({
            "messageType": "RichText/Html",
            "content": (
                '<URIObject type="SWIFT.1"><Title>Visible fallback</Title>'
                '<Swift b64="not-valid-base64-%" /><Description />'
                '</URIObject>'
            ),
        })

        self.assertIn("Visible fallback", text)
        self.assertNotIn("not-valid-base64", text)
        self.assertNotIn("b64", text)

    def test_plain_message_content_with_swift_uriobject_is_sanitized(self):
        card = {
            "type": "AdaptiveCard",
            "body": [{"type": "TextBlock", "text": "Decoded plain message"}],
        }
        encoded = base64.b64encode(json.dumps(card).encode("utf-8")).decode("ascii")
        text = self.formatter.format_message({
            "messageType": "Text",
            "content": (
                '<URIObject type="SWIFT.1"><Title>Visible plain text</Title>'
                f'<Swift b64="{encoded}" /></URIObject>'
            ),
        })
        self.assertIn("Visible plain text", text)
        self.assertIn("Decoded plain message", text)
        self.assertNotIn(encoded, text)

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

            last_message_read_coverage = {"retry_count": 0}

            def iter_message_pages(
                self,
                conversation_id,
                limit=None,
                since=None,
                until=None,
                cancel_event=None,
                progress_callback=None,
            ):
                self.calls.append((conversation_id, limit, since, until))
                page = [{
                    "id": "one",
                    "sequenceId": 1,
                    "originalarrivaltime": "2026-09-28T13:00:00+03:00",
                    "imdisplayname": "Taylor",
                    "messagetype": "RichText/Html",
                    "content": "<p>Hi <b>there</b></p>",
                }]
                if progress_callback:
                    progress_callback(1, len(page), 0)
                yield page

        connector = FakeConnector()
        exporter = TeamsExporter(connector, TeamsMessageFormatter())
        chat = ChatSummary("19:fake", "Sample chat")
        since = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
        until = datetime(2026, 9, 28, 11, 0, tzinfo=timezone.utc)
        destination = Path(self.id().replace(".", "_") + ".txt")
        try:
            output = exporter.export_chat(
                chat,
                destination,
                limit=None,
                since=since,
                until=until,
            )
            content = output.read_text(encoding="utf-8-sig")
            self.assertIn("Conversation: Sample chat", content)
            self.assertIn("Conversation ID: 19:fake", content)
            self.assertIn("[2026-09-28T10:00:00Z] Taylor", content)
            self.assertIn("Hi **there**", content)
            self.assertIn(f"Since: {since.isoformat()}", content)
            self.assertIn(f"Until: {until.isoformat()}", content)
            self.assertEqual(
                connector.calls[0], ("19:fake", None, since, until)
            )
        finally:
            destination.unlink(missing_ok=True)

    def test_export_sanitizes_swift_payloads_in_preformatted_archived_bodies(self):
        card = {
            "type": "AdaptiveCard",
            "body": [{"type": "TextBlock", "text": "Archived card text"}],
        }
        encoded = base64.b64encode(json.dumps(card).encode("utf-8")).decode("ascii")

        class ArchivedConnector:
            last_message_read_coverage = {"retry_count": 0}

            def iter_message_pages(self, *_args, **_kwargs):
                yield [{
                    "id": "archived-card",
                    "originalarrivaltime": "2026-09-28T10:00:00Z",
                    "imdisplayname": "Taylor",
                    "__formatted_body": (
                        '<URIObject type="SWIFT.1"><Title>Visible archived text</Title>'
                        f'<Swift b64="{encoded}" /></URIObject>'
                    ),
                }]

        destination = Path(self.id().replace(".", "_") + ".txt")
        try:
            exporter = TeamsExporter(ArchivedConnector(), TeamsMessageFormatter())
            exporter.export_chat(ChatSummary("19:archive", "Archive"), destination)
            content = destination.read_text(encoding="utf-8-sig")
            self.assertIn("Visible archived text", content)
            self.assertIn("Archived card text", content)
            self.assertNotIn(encoded, content)
            self.assertNotIn("b64", content)
        finally:
            destination.unlink(missing_ok=True)

    def test_large_export_spools_pages_to_disk_and_replays_chronologically(self):
        class LargeConnector:
            last_message_read_coverage = {"retry_count": 0}

            def iter_message_pages(self, *_args, **_kwargs):
                for index in reversed(range(80)):
                    yield [{
                        "id": f"message-{index}",
                        "sequenceId": index,
                        "originalarrivaltime": (
                            f"2026-09-28T10:{index // 60:02d}:{index % 60:02d}Z"
                        ),
                        "imdisplayname": f"Author {index}",
                        "messagetype": "Text",
                        "content": "x" * 20_000,
                    }]

        import teams_exporter.teams_exporter as exporter_module

        original_spool = exporter_module.tempfile.SpooledTemporaryFile
        spools = []

        def tracked_spool(*args, **kwargs):
            spool = original_spool(*args, **kwargs)
            spools.append(spool)
            return spool

        destination = Path(self.id().replace(".", "_") + ".large.txt")
        exporter = TeamsExporter(LargeConnector(), TeamsMessageFormatter())
        try:
            with patch(
                "teams_exporter.teams_exporter.tempfile.SpooledTemporaryFile",
                side_effect=tracked_spool,
            ):
                exporter.export_chat(ChatSummary("19:large", "Large"), destination)
            content = destination.read_text(encoding="utf-8")
            self.assertTrue(spools[0]._rolled)
            self.assertGreater(destination.stat().st_size, 1_000_000)
            self.assertLess(content.index("[2026-09-28T10:00:00Z] Author 0"),
                            content.index("[2026-09-28T10:01:19Z] Author 79"))
        finally:
            destination.unlink(missing_ok=True)

    def test_cancelled_export_keeps_existing_destination_intact(self):
        class CancelConnector:
            last_message_read_coverage = {"retry_count": 0}

            def iter_message_pages(self, *_args, **kwargs):
                yield [{
                    "id": "finished-page",
                    "originalarrivaltime": "2026-09-28T10:00:00Z",
                    "imdisplayname": "Taylor",
                    "messagetype": "Text",
                    "content": "already fetched",
                }]
                kwargs["cancel_event"].set()
                raise ExtractionCancelled("cancelled")

        destination = Path(self.id().replace(".", "_") + ".cancel.txt")
        destination.write_text("previous export", encoding="utf-8")
        try:
            exporter = TeamsExporter(CancelConnector(), TeamsMessageFormatter())
            with self.assertRaises(ExtractionCancelled):
                exporter.export_chat(
                    ChatSummary("19:cancel", "Cancel"),
                    destination,
                    cancel_event=threading.Event(),
                )
            self.assertEqual(destination.read_text(encoding="utf-8"), "previous export")
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])
        finally:
            destination.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()


