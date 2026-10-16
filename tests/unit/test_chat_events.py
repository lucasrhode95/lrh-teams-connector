from __future__ import annotations

import json

from teams_connector.remote import chat_service_mapping
from teams_connector.remote.chat_events import render

ORGID_ONE = "8:orgid:00000000-0000-0000-0000-000000000001"
ORGID_TWO = "8:orgid:00000000-0000-0000-0000-000000000002"
ORGID_THREE = "8:orgid:00000000-0000-0000-0000-000000000003"
ENDED = (
    '<ended/><partlist alt="" count="3">'
    f'<part identity="{ORGID_ONE}"><name>{ORGID_ONE}</name><displayName>Alex Kim</displayName>'
    "<duration>2502</duration></part>"
    f'<part identity="{ORGID_TWO}"><name>{ORGID_TWO}</name><displayName>Sam Lee</displayName>'
    "<duration>2502</duration></part>"
    f'<part identity="{ORGID_THREE}"><name>{ORGID_THREE}</name><displayName>Jo Park</displayName>'
    "<duration>2502</duration></part>"
    "</partlist><meetingDetails><meetingDetails><organizerUpn>organizer@example.invalid</organizerUpn>"
    "<instanceDetails><subject>Weekly sync</subject></instanceDetails></meetingDetails></meetingDetails>"
)
STARTED = (
    '<partlist alt=""></partlist><meetingDetails><meetingDetails>'
    "<organizerUpn>organizer@example.invalid</organizerUpn><meetingType>Recurring</meetingType>"
    "<instanceDetails><iCalUid>synthetic-ical-id</iCalUid><subject>Weekly sync</subject>"
    "<type>Occurrence</type></instanceDetails></meetingDetails></meetingDetails>"
)


def _members(*names: str) -> str:
    record = {
        "eventtime": 1791396084173,
        "initiator": ORGID_ONE,
        "members": [{"id": ORGID_TWO, "friendlyname": name, "meetingMemberType": "temp"} for name in names],
    }
    return json.dumps(record)


def test_ended_meeting_shows_duration_and_participants_only() -> None:
    line = render("Event/Call", ENDED)
    assert line == "[Meeting ended · 41m 42s · Participants: Alex Kim; Sam Lee; Jo Park]"
    assert "<" not in line and "orgid" not in line and "organizer" not in line


def test_long_meeting_shows_hours() -> None:
    long_call = ENDED.replace("<duration>2502</duration>", "<duration>3900</duration>")
    assert (
        render("Event/Call", long_call)
        == "[Meeting ended · 1h 05m · Participants: Alex Kim; Sam Lee; Jo Park]"
    )


def test_started_meeting_shows_its_subject_or_nothing_more() -> None:
    assert render("Event/Call", STARTED) == "[Meeting started: Weekly sync]"
    assert render("Event/Call", '<partlist alt=""></partlist>') == "[Meeting started]"


def test_members_joined_and_left_name_the_members() -> None:
    assert render("ThreadActivity/MemberJoined", _members("Sam Lee")) == "[Sam Lee joined]"
    assert (
        render("ThreadActivity/MemberJoined", _members("Sam Lee", "Jo Park")) == "[Sam Lee; Jo Park joined]"
    )
    assert render("ThreadActivity/MemberLeft", _members("Sam Lee")) == "[Sam Lee left]"
    assert render("ThreadActivity/MemberJoined", _members("")) == "[A member joined]"


def test_member_added_and_topic_update() -> None:
    assert render("ThreadActivity/AddMember", "<addmember/>") == "[Member added]"
    topic = "<topicupdate><value>Synthetic topic</value></topicupdate>"
    assert render("ThreadActivity/TopicUpdate", topic) == "[Topic changed to: Synthetic topic]"


def test_unknown_or_unreadable_records_render_their_type_never_their_content() -> None:
    policy = "<meetingpolicyupdated><value>ChatEnabled</value></meetingpolicyupdated>"
    assert render("ThreadActivity/MeetingPolicyUpdated", policy) == "[ThreadActivity/MeetingPolicyUpdated]"
    assert render("Event/Call", "<ended/><partlist") == "[Event/Call]"
    assert render("ThreadActivity/MemberJoined", "{not json") == "[ThreadActivity/MemberJoined]"


def test_mapping_uses_the_one_line_for_event_records() -> None:
    raw = {
        "id": "3",
        "messagetype": "Event/Call",
        "content": ENDED,
        "originalarrivaltime": "2026-09-15T19:12:45.479Z",
    }
    message = chat_service_mapping.to_message(raw, "c")
    assert message.text == "[Meeting ended · 41m 42s · Participants: Alex Kim; Sam Lee; Jo Park]"
    assert message.system_event == "Event/Call" and not message.attachments


def test_a_transcript_notice_is_one_line_and_its_json_is_never_shown() -> None:
    notice = json.dumps(
        {"scopeId": "synthetic-scope", "storageId": "synthetic-storage", "isExportedToOdsp": True}
    )
    message = chat_service_mapping.to_message(
        {"id": "4", "messagetype": "RichText/Media_CallTranscript", "content": notice}, "c"
    )
    assert message.text == "[Transcript saved]"
    assert message.system_event == "RichText/Media_CallTranscript"
    assert render("RichText/Media_CallTranscript", notice) == "[Transcript saved]"
