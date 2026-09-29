import unittest
from urllib.parse import parse_qs, urlsplit

from teams_exporter.teams_connector import TeamsConnector

class StubConnector(TeamsConnector):
    def __init__(self, pages):
        super().__init__()
        self._access_token = "test"
        self._skype_token = "test"
        self._authz_response = {
            "regionGtms": {"chatService": "https://chat.example.test"}
        }
        self.pages = iter(pages)
        self.requests = []

    def _request_json(self, url, method="GET", body=None):
        self.requests.append(url)
        return next(self.pages)

    def _populate_conversation_display_names(self, conversations):
        pass


def page(start, end, link=None):
    return {
        "conversations": [
            {"id": str(n), "version": 1000 - n}
            for n in range(start, end + 1)
        ],
        "_metadata": {"backwardLink": link},
    }


class PagingTests(unittest.TestCase):
    def test_chat_listing_skips_service_streams_and_empty_meetings(self):
        link = "https://chat.example.test/v1/users/ME/conversations?cursor=older"
        client = StubConnector([
            {
                "conversations": [
                    {"id": "notifications", "version": 100,
                     "threadProperties": {"productThreadType": "StreamOfNotifications"}},
                    {"id": "empty-meeting", "version": 99,
                     "threadProperties": {"productThreadType": "Meeting", "hidden": "false"},
                     "properties": {"isemptyconversation": "True"}},
                    {"id": "chat-one", "version": 98,
                     "threadProperties": {"productThreadType": "OneToOneChat"}},
                ],
                "_metadata": {"backwardLink": link},
            },
            {
                "conversations": [
                    {"id": "channel", "version": 97,
                     "threadProperties": {"productThreadType": "TeamsStandardChannel"}},
                    {"id": "chat-two", "version": 96,
                     "threadProperties": {"productThreadType": "Chat"}},
                ]
            },
        ])
        self.assertEqual(
            [row["id"] for row in client.get_chats(1, 2)],
            ["chat-one", "chat-two"],
        )
        self.assertEqual(
            [row["id"] for row in client.get_conversations(1, 2)],
            ["notifications", "empty-meeting"],
        )

    def test_range_reuses_snapshot_and_fetches_only_missing_rows(self):
        link = "https://chat.example.test/v1/users/ME/conversations?cursor=older"
        client = StubConnector([page(1, 10, link), page(11, 20)])
        self.assertEqual(len(client.get_conversations(1, 10)), 10)
        self.assertEqual(len(client.get_conversations(11, 20)), 10)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["pageSize"][0] for url in client.requests],
            ["10", "10"],
        )

    def test_large_range_uses_bounded_pages(self):
        link = "https://chat.example.test/v1/users/ME/conversations?cursor=older"
        client = StubConnector([page(1, 500, link), page(501, 600)])
        self.assertEqual(len(client.get_conversations(1, 600)), 600)
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["pageSize"][0] for url in client.requests],
            ["500", "100"],
        )

    def test_repeated_continuation_raises_instead_of_claiming_completion(self):
        link = "https://chat.example.test/v1/users/ME/conversations?cursor=same"
        client = StubConnector([page(1, 1, link), page(1, 1, link)])
        with self.assertRaisesRegex(RuntimeError, "repeated a conversation"):
            client.get_conversations(1, 3)
        self.assertFalse(client._conversation_snapshot_complete)

    def test_three_duplicate_only_pages_raise(self):
        links = [
            f"https://chat.example.test/v1/users/ME/conversations?cursor={n}"
            for n in range(4)
        ]
        client = StubConnector([
            page(1, 1, links[0]),
            page(1, 1, links[1]),
            page(1, 1, links[2]),
            page(1, 1, links[3]),
        ])
        with self.assertRaisesRegex(RuntimeError, "without progress"):
            client.get_conversations(1, 3)

    def test_equal_versions_keep_service_order(self):
        client = StubConnector([{
            "conversations": [
                {"id": "a", "version": 10},
                {"id": "z", "version": 10},
                {"id": "b", "version": 9},
            ]
        }])
        self.assertEqual(
            [row["id"] for row in client.get_conversations(1, 3)],
            ["a", "z", "b"],
        )


if __name__ == "__main__":
    unittest.main()
