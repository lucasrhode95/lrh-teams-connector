"""The meeting summary's entity id, its request, and its answer, from synthetic ids and answers only."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import httpx
import pytest

from teams_connector import config
from teams_connector.domain.errors import Upstream
from teams_connector.domain.models import RecordingRef
from teams_connector.remote.substrate_summaries import (
    SubstrateMeetingSummaries,
    entity_id,
    readable_summary,
    summary_text,
)
from teams_connector.remote.transport import Transport

SITE = "11111111-2222-3333-4444-555555555555"
WEB = "66666666-7777-8888-9999-aaaaaaaaaaaa"
LIBRARY = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
ITEM = "01SYNTHITEM"


def _drive_id(*guids: str) -> str:
    """A drive id as SharePoint writes it: ``b!`` and the base64url of the GUIDs' 16-byte blocks."""
    blocks = b"".join(uuid.UUID(guid).bytes_le for guid in guids)
    return "b!" + base64.urlsafe_b64encode(blocks).decode("ascii").rstrip("=")


def _expected_entity(*guids: str, item: str = ITEM) -> str:
    """The entity id in its text form: the GUIDs joined by commas, base64url-encoded, then the item id."""
    middle = base64.urlsafe_b64encode(",".join(guids).encode("ascii")).decode("ascii").rstrip("=")
    return f"SPO_{middle}_{item}"


def _answer(source: dict[str, Any]) -> dict[str, Any]:
    return {"EntitySets": [{"ResultSets": [{"Results": [{"Source": source}], "Total": 1}]}]}


class _Credential:
    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        return {"Authorization": "Bearer synthetic"}

    def sign_in_command(self) -> str:
        return "teams-connector auth"


def test_the_entity_id_carries_the_site_web_and_library_in_order() -> None:
    assert entity_id(_drive_id(SITE, WEB, LIBRARY), ITEM) == _expected_entity(SITE, WEB, LIBRARY)


@pytest.mark.parametrize(
    ("drive_id", "item_id"),
    [
        ("b!SYNTH-DRIVE", ITEM),
        (_drive_id(SITE, WEB, LIBRARY), "item with spaces"),
        ("x" + _drive_id(SITE, WEB, LIBRARY), ITEM),
    ],
)
def test_ids_of_another_shape_are_refused(drive_id: str, item_id: str) -> None:
    with pytest.raises(Upstream):
        entity_id(drive_id, item_id)


async def test_the_request_names_the_meeting_and_the_summary_scenario() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"EntitySets": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        reader = SubstrateMeetingSummaries(
            Transport(client=client), _Credential(), anchor_mailbox="Oid:synthetic@tenant"
        )
        found = await reader.summary(RecordingRef(drive_id=_drive_id(SITE, WEB, LIBRARY), item_id=ITEM))

    assert found is None
    [request] = sent
    assert str(request.url) == config.MEETING_SUMMARY_URL
    assert request.headers["x-anchormailbox"] == "Oid:synthetic@tenant"
    assert json.loads(request.content) == {
        "entityRequests": [
            {
                "context": {"entityId": _expected_entity(SITE, WEB, LIBRARY)},
                "queryParameters": [{"entityType": "MeetingCatchUp"}],
            }
        ],
        "scenario": {"name": "MeetingCatchup"},
    }


def test_the_summary_is_the_meeting_summary_of_the_first_result() -> None:
    answer = _answer({"MeetingSummary": "Synthetic summary.", "Headline": "Synthetic headline."})

    assert summary_text(answer) == "Synthetic summary."


def test_a_processing_state_under_the_same_name_is_not_the_summary() -> None:
    assert summary_text(_answer({"InsightProcessingStates": {"MeetingSummary": "Completed"}})) is None


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [],
        {},
        {"EntitySets": "not a list"},
        {"EntitySets": [{"ResultSets": [{"Total": 0}]}]},
        _answer({"MeetingSummary": "   "}),
    ],
)
def test_an_answer_without_a_summary_is_none(answer: Any) -> None:
    assert summary_text(answer) is None


def _topics(*topics: dict[str, Any]) -> str:
    return json.dumps(
        {"KeyTopics": list(topics), "Version": "2.0.0", "ProcessingResult": "ProcessedSuccessfully"}
    )


def test_a_key_topics_document_reads_as_text() -> None:
    document = _topics(
        {
            "Headline": "Synthetic topic",
            "Summary": "Synthetic summary.",
            "DetailedSummaries": [
                {"topic": "First point", "text": "Synthetic detail."},
                {"topic": "Second point", "text": "Another detail."},
            ],
            "Citations": [{"UtteranceId": "synthetic/1", "Source": "Transcript"}],
        }
    )
    assert readable_summary(document) == (
        "Synthetic topic\nSynthetic summary.\n"
        "- First point: Synthetic detail.\n- Second point: Another detail."
    )


def test_topics_are_separated_by_a_blank_line() -> None:
    document = _topics({"Headline": "One", "Summary": "First."}, {"Headline": "Two", "Summary": "Second."})
    assert readable_summary(document) == "One\nFirst.\n\nTwo\nSecond."


def test_a_document_without_topics_is_no_summary() -> None:
    assert readable_summary(_topics()) is None


def test_text_that_is_not_the_document_is_returned_as_it_is() -> None:
    assert readable_summary("Synthetic plain summary.") == "Synthetic plain summary."
    assert readable_summary('{"Other": 1}') == '{"Other": 1}'


def test_the_summary_answer_is_read_as_text() -> None:
    answer = _answer(
        {"MeetingSummary": _topics({"Headline": "Synthetic topic", "Summary": "Synthetic summary."})}
    )
    assert summary_text(answer) == "Synthetic topic\nSynthetic summary."
