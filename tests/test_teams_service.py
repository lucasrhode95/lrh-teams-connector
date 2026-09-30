from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from teams_exporter.teams_connector import (
    AuthenticationRequiredError,
    ChatCatalogCoverage,
    ChatSummary,
    TeamsConnector,
)
from teams_exporter.teams_search_index import TeamsSearchIndex
from teams_exporter.teams_service import InvalidCursorError, TeamsService


def message(identifier: str, second: int, content: str, author: str = "A Sender"):
    return {
        "id": identifier,
        "sequenceId": second,
        "originalarrivaltime": f"2026-09-28T10:00:{second:02d}Z",
        "imdisplayname": author,
        "content": content,
        "messagetype": "Text",
    }


class ServiceConnector(TeamsConnector):
    def __init__(self):
        super().__init__()
        latest = datetime(2026, 9, 28, 10, 0, 3, tzinfo=timezone.utc)
        self.chats = [
            ChatSummary(
                "19:alpha@thread.v2",
                "Alpha",
                last_message_at=latest,
                chat_type="group",
                avatar_url="https://images.example.test/alpha.png",
            ),
            ChatSummary("19:beta@thread.v2", "Beta", last_message_at=latest, chat_type="oneOnOne"),
        ]
        self.pages = {}
        self.members = {}
        self.page_calls = []
        self.retry_counts = []
        self.login_calls = 0
        self.auth_required = False
        self.sent_messages = []

    def login(self, *, interactive=True):
        self.login_calls += 1
        if self.auth_required:
            raise AuthenticationRequiredError("sign-in needed")

    def list_chats(self):
        return self.chats

    def get_messages_page(
        self,
        conversation_id,
        *,
        page_size=50,
        since=None,
        until=None,
        continuation_url=None,
    ):
        self.page_calls.append((conversation_id, page_size, since, until, continuation_url))
        self.last_request_retry_count = (
            self.retry_counts.pop(0) if self.retry_counts else 0
        )
        pages = self.pages.get(conversation_id, [([], None)])
        index = 0 if continuation_url is None else int(continuation_url.rsplit(":", 1)[1])
        page, next_url = pages[index]
        return page, next_url, len(page), 0

    def get_conversation_members(self, conversation_id):
        if conversation_id not in self.members:
            raise RuntimeError("roster unavailable")
        return self.members[conversation_id]

    def send_message(self, conversation_id, text):
        self.sent_messages.append((conversation_id, text))
        return {
            "message_id": "sent-message-id",
            "client_message_id": "1790123456789",
            "sent_at": "2026-09-22T00:30:56.789Z",
        }


class TeamsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connector = ServiceConnector()
        index = TeamsSearchIndex(Path(self.temp.name) / "search.sqlite3")
        self.service = TeamsService(connector=self.connector, index=index)

    def test_list_chats_returns_bounded_pages_and_coverage(self):
        self.connector.catalog_coverage = ChatCatalogCoverage(
            catalog_complete=False,
            catalog_source="sanitized fixture",
            catalog_scope="Fixture chats only.",
            csa_reported_partial=True,
            chat_service_unique_rows=4,
            fallback_chats_added=1,
            unclassified_service_rows_excluded=1,
        )
        first = self.service.list_chats(page_size=1)
        second = self.service.list_chats(page_size=1, page_token=first["next_page_token"])

        self.assertEqual([row["id"] for row in first["items"]], ["19:alpha@thread.v2"])
        self.assertEqual([row["id"] for row in second["items"]], ["19:beta@thread.v2"])
        self.assertIsNone(second["next_page_token"])
        self.assertFalse(first["coverage"]["catalog_complete"])
        self.assertEqual(first["coverage"]["catalog_source"], "sanitized fixture")
        self.assertEqual(first["coverage"]["chat_service_unique_rows"], 4)
        self.assertEqual(first["coverage"]["fallback_chats_added"], 1)
        self.assertEqual(first["coverage"]["unclassified_service_rows_excluded"], 1)
        self.assertEqual(self.service.index.counts()["catalog_chats"], 2)

    def test_avatar_uris_are_opt_in_for_local_ui_and_omitted_by_default(self):
        default_result = self.service.list_chats(page_size=1)
        ui_result = self.service.list_chats(page_size=1, include_avatar_uris=True)

        self.assertNotIn("avatar_url", default_result["items"][0])
        self.assertEqual(
            ui_result["items"][0]["avatar_url"],
            "https://images.example.test/alpha.png",
        )

    def test_missing_account_picture_uri_does_not_block_account_setup(self):
        service = TeamsService(connector=self.connector)
        service._index_path = Path(self.temp.name) / "account-archive.sqlite3"

        service._select_index_for_account(
            {
                "username": "taylor@example.test",
                "name": "Taylor Example",
                "tenant_id": "tenant-guid",
                "user_id": "user-guid",
            }
        )

        self.assertEqual(service.account_info["name"], "Taylor Example")
        self.assertNotIn("picture_url", service.account_info)
        self.assertTrue(service.index.owner_fingerprint)

    def test_get_messages_uses_cursor_and_deduplicates_overlapping_pages(self):
        self.connector.pages["19:alpha@thread.v2"] = [
            ([message("m2", 2, "second"), message("m3", 3, "third")], "page:1"),
            ([message("m1", 1, "first"), message("m2", 2, "second")], None),
        ]

        first = self.service.get_messages("19:alpha@thread.v2", page_size=2)
        second = self.service.get_messages(
            "19:alpha@thread.v2", page_size=2, cursor=first["next_cursor"]
        )

        self.assertEqual([row["id"] for row in first["items"]], ["m2", "m3"])
        self.assertEqual([row["id"] for row in second["items"]], ["m1"])
        self.assertTrue(second["coverage"]["complete"])
        self.assertEqual(second["coverage"]["raw_items_scanned_total"], 4)

    def test_get_messages_reports_retry_count_for_each_page_request(self):
        self.connector.pages["19:alpha@thread.v2"] = [
            ([message("m2", 2, "second")], "page:1"),
            ([message("m1", 1, "first")], None),
        ]
        self.connector.retry_counts = [2, 1]

        first = self.service.get_messages("19:alpha@thread.v2")
        second = self.service.get_messages(
            "19:alpha@thread.v2", cursor=first["next_cursor"]
        )

        self.assertEqual(first["coverage"]["retry_count_this_call"], 2)
        self.assertEqual(second["coverage"]["retry_count_this_call"], 1)

    def test_get_messages_marks_per_message_content_truncation_explicitly(self):
        long_body = "x" * 1_250
        self.connector.pages["19:alpha@thread.v2"] = [
            ([message("long", 1, long_body)], None)
        ]

        result = self.service.get_messages("19:alpha@thread.v2")

        item = result["items"][0]
        self.assertEqual(len(item["content"]), 1_200)
        self.assertTrue(item["content_truncated"])
        self.assertEqual(item["content_char_count"], 1_250)

    def test_mcp_message_timestamps_are_normalized_to_utc(self):
        localized_message = message("localized", 1, "time zone probe")
        localized_message["originalarrivaltime"] = "2026-09-28T13:00:01+03:00"
        self.connector.pages["19:alpha@thread.v2"] = [([localized_message], None)]

        result = self.service.get_messages("19:alpha@thread.v2")

        self.assertEqual(result["items"][0]["timestamp"], "2026-09-28T10:00:01Z")

    def test_get_messages_cursor_is_bound_to_conversation_and_dates(self):
        self.connector.pages["19:alpha@thread.v2"] = [
            ([message("m1", 1, "first")], "page:1"),
            ([message("m2", 2, "second")], None),
        ]
        since = datetime(2026, 9, 28, 9, tzinfo=timezone.utc)
        first = self.service.get_messages("19:alpha@thread.v2", since=since)

        with self.assertRaises(InvalidCursorError):
            self.service.get_messages("19:beta@thread.v2", cursor=first["next_cursor"])

    def test_index_sync_resumes_deduplicates_and_refreshes_without_double_counting(self):
        self.connector.pages["19:alpha@thread.v2"] = [
            ([message("m1", 1, "older item"), message("m2", 2, "deployment update")], "page:1"),
            ([message("m2", 2, "deployment update"), message("m3", 3, "newer item")], None),
        ]
        self.connector.members["19:alpha@thread.v2"] = [
            {"id": "8:orgid:1", "userDisplayName": "Taylor Example"}
        ]
        self.connector.retry_counts = [2, 1, 0]

        first = self.service.sync_search_index(
            conversation_id="19:alpha@thread.v2",
            max_messages_to_fetch=2,
            max_pages_to_fetch=1,
        )
        second = self.service.sync_search_index(
            conversation_id="19:alpha@thread.v2",
            max_messages_to_fetch=2,
            max_pages_to_fetch=1,
        )
        third = self.service.sync_search_index(
            conversation_id="19:alpha@thread.v2",
            max_messages_to_fetch=2,
            max_pages_to_fetch=1,
        )

        self.assertFalse(first["items"][0]["message_history_complete"])
        self.assertEqual(first["items"][0]["messages_added_this_call"], 2)
        self.assertEqual(first["items"][0]["message_retries_this_call"], 2)
        self.assertTrue(second["items"][0]["message_history_complete"])
        self.assertEqual(second["items"][0]["messages_added_this_call"], 1)
        self.assertEqual(second["items"][0]["message_retries_this_call"], 1)
        self.assertEqual(second["items"][0]["messages_stored_total"], 3)
        self.assertEqual(third["items"][0]["messages_added_this_call"], 0)
        self.assertTrue(third["items"][0]["message_history_complete"])
        self.assertEqual([call[4] for call in self.connector.page_calls], [None, "page:1", None])

        result = self.service.search_messages("deployment update", conversation_id="19:alpha@thread.v2")
        self.assertEqual(result["total_matches_in_index"], 1)
        self.assertEqual(result["items"][0]["id"], "m2")
        self.assertEqual(result["items"][0]["author"], "A Sender")
        self.assertTrue(result["coverage"]["complete_for_requested_scope"])

        participants = self.service.search_chats("taylor")
        self.assertEqual([item["id"] for item in participants["items"]], ["19:alpha@thread.v2"])
        self.assertFalse(participants["coverage"]["complete_for_chat_catalog"])

    def test_sqlite_history_and_continuation_survive_service_recreation(self):
        self.connector.pages["19:alpha@thread.v2"] = [
            ([message("m2", 2, "older retained deployment")], "page:1"),
            ([message("m3", 3, "newer retained item")], None),
        ]
        self.connector.members["19:alpha@thread.v2"] = []
        index_path = Path(self.temp.name) / "persistent.sqlite3"
        first_service = TeamsService(
            connector=self.connector,
            index=TeamsSearchIndex(index_path),
        )

        first = first_service.sync_search_index(
            conversation_id="19:alpha@thread.v2",
            max_messages_to_fetch=1,
            max_pages_to_fetch=1,
            include_participants=False,
        )
        resumed_service = TeamsService(
            connector=self.connector,
            index=TeamsSearchIndex(index_path),
        )
        second = resumed_service.sync_search_index(
            conversation_id="19:alpha@thread.v2",
            max_messages_to_fetch=1,
            max_pages_to_fetch=1,
            include_participants=False,
        )
        reopened_service = TeamsService(
            connector=self.connector,
            index=TeamsSearchIndex(index_path),
        )

        self.assertFalse(first["items"][0]["message_history_complete"])
        self.assertTrue(second["items"][0]["message_history_complete"])
        result = reopened_service.search_messages(
            "older retained deployment", conversation_id="19:alpha@thread.v2"
        )
        self.assertEqual([item["id"] for item in result["items"]], ["m2"])
        self.assertTrue(result["coverage"]["complete_for_requested_scope"])

    def test_bounded_all_chat_sync_rotates_fairly_across_catalog(self):
        first = self.service.sync_search_index(
            max_chats_to_sync=1,
            max_messages_to_fetch=1,
            max_pages_to_fetch=1,
            include_participants=False,
            refresh_latest=False,
        )
        second = self.service.sync_search_index(
            max_chats_to_sync=1,
            max_messages_to_fetch=1,
            max_pages_to_fetch=1,
            include_participants=False,
            refresh_latest=False,
        )

        self.assertEqual(first["items"][0]["chat_id"], "19:alpha@thread.v2")
        self.assertEqual(second["items"][0]["chat_id"], "19:beta@thread.v2")

    def test_unresolved_participant_names_keep_chat_search_coverage_incomplete(self):
        self.connector.members["19:alpha@thread.v2"] = [
            {"id": "8:orgid:known", "userDisplayName": "Taylor Example"},
            {"id": "8:orgid:unknown", "userDisplayName": ""},
        ]

        synced = self.service.sync_search_index(
            conversation_id="19:alpha@thread.v2",
            max_messages_to_fetch=1,
            max_pages_to_fetch=1,
        )
        result = self.service.search_chats("taylor")

        self.assertEqual(synced["items"][0]["unresolved_participants"], 1)
        self.assertEqual(result["coverage"]["unresolved_participants"], 1)
        self.assertFalse(result["coverage"]["complete_for_chat_catalog"])

    def test_status_reports_date_range_coverage_per_chat(self):
        self.connector.pages["19:alpha@thread.v2"] = [
            ([message("m1", 1, "one"), message("m2", 2, "two"), message("m3", 3, "three")], None)
        ]
        self.service.sync_search_index(
            conversation_id="19:alpha@thread.v2", include_participants=False
        )
        since = datetime(2026, 9, 28, 10, 0, 2, tzinfo=timezone.utc)
        until = datetime(2026, 9, 28, 10, 0, 3, tzinfo=timezone.utc)

        result = self.service.get_search_index_status(
            conversation_id="19:alpha@thread.v2", since=since, until=until
        )

        self.assertTrue(result["coverage"]["complete_for_requested_scope"])
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["items"][0]["requested_range_covered"])
        self.assertNotIn("messages_next_url", result["items"][0])

    def test_participant_search_does_not_claim_complete_when_rosters_are_missing(self):
        self.service.list_chats()
        result = self.service.search_chats("nobody")

        self.assertEqual(result["items"], [])
        self.assertFalse(result["coverage"]["complete_for_chat_catalog"])
        self.assertEqual(result["coverage"]["rosters_not_indexed"], 2)

    def test_authentication_needed_is_silent_and_actionable(self):
        self.connector.auth_required = True

        with self.assertRaisesRegex(AuthenticationRequiredError, "sign-in needed"):
            self.service.list_chats()

        self.assertEqual(self.connector.login_calls, 1)

    def test_send_message_requires_authorization_and_catalog_name_match(self):
        with self.assertRaisesRegex(ValueError, "explicit send authorization"):
            self.service.send_message(
                "19:alpha@thread.v2",
                "Alpha",
                "Hello",
                user_confirmation="",
            )
        self.assertEqual(self.connector.login_calls, 0)

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.service.send_message(
                "19:alpha@thread.v2",
                "Beta",
                "Hello",
                user_confirmation="User explicitly authorized this message.",
            )
        self.assertEqual(self.connector.sent_messages, [])

    def test_send_message_rejects_conversation_missing_from_live_catalog(self):
        with self.assertRaisesRegex(ValueError, "not found in the current Teams chat catalog"):
            self.service.send_message(
                "19:missing@thread.v2",
                "Missing chat",
                "Hello",
                user_confirmation="User explicitly authorized sending this message.",
            )

        self.assertEqual(self.connector.sent_messages, [])

    def test_send_message_targets_the_verified_chat_and_returns_sanitized_receipt(self):
        result = self.service.send_message(
            "19:alpha@thread.v2",
            "Alpha",
            "Hello from an authorized test.",
            user_confirmation="User explicitly authorized sending this test to Alpha.",
        )

        self.assertEqual(
            self.connector.sent_messages,
            [("19:alpha@thread.v2", "Hello from an authorized test.")],
        )
        self.assertEqual(result["conversation_id"], "19:alpha@thread.v2")
        self.assertEqual(result["chat_name"], "Alpha")
        self.assertEqual(result["message_id"], "sent-message-id")
        self.assertEqual(result["client_message_id"], "1790123456789")
        self.assertEqual(result["delivery_status"], "accepted")
        self.assertNotIn("user_confirmation", result)

    def test_local_search_does_not_start_authentication_or_wait_for_sign_in(self):
        result = self.service.search_messages("anything")
        self.assertEqual(result["items"], [])
        self.assertFalse(result["coverage"]["complete_for_requested_scope"])
        self.assertEqual(self.connector.login_calls, 0)

    def test_invalid_or_mismatched_search_continuation_is_rejected(self):
        self.connector.pages["19:alpha@thread.v2"] = [
            ([message("m1", 1, "deployment alpha"), message("m2", 2, "deployment beta")], None)
        ]
        self.service.sync_search_index(
            conversation_id="19:alpha@thread.v2", include_participants=False
        )
        first = self.service.search_messages(
            "deployment", conversation_id="19:alpha@thread.v2", result_limit=1
        )
        self.assertIsNotNone(first["next_cursor"])
        with self.assertRaises(InvalidCursorError):
            self.service.search_messages("another query", cursor=first["next_cursor"])


if __name__ == "__main__":
    unittest.main()
