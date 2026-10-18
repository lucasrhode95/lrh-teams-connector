"""Meeting recording cards read as one marker line: title and play link, never media-service URLs.

Synthetic fixtures only: the hosts, ids and titles are invented.
"""

from __future__ import annotations

from teams_connector.remote import chat_service_mapping
from teams_connector.service.export import formatter

AMS = "https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/0-weu-d1-0123456789abcdef0123456789abcdef"
PLAY = "https://contoso-my.sharepoint.com/:v:/p/mia/recording-1"


def _recording(status: str = "Success", link: bool = True) -> dict[str, str]:
    anchor = f'<a href="{PLAY}">Play</a>' if link else ""
    content = (
        f'<URIObject type="Video.2/CallRecording.1" uri="" url_thumbnail="{AMS}/views/thumbnail_small">'
        f'<RecordingStatus status="{status}" code="200"></RecordingStatus>'
        f"<Title>Weekly sync &amp; review</Title>{anchor}"
        '<OriginalName v="Weekly sync-20260901.mp4"></OriginalName>'
        f'<RecordingContent><item type="amsVideo" uri="{AMS}/views/video"></item>'
        f'<item type="onedriveForBusinessVideo" uri="{PLAY}"></item></RecordingContent></URIObject>'
    )
    return {"id": "r1", "messagetype": "RichText/Media_CallRecording", "content": content}


def test_a_recording_reads_as_its_title_and_play_link() -> None:
    message = chat_service_mapping.to_message(_recording(), "19:meeting@thread.v2")
    assert message.text == f"[Recording: Weekly sync & review] {PLAY}"


def test_a_recording_without_a_link_yet_shows_its_title_only() -> None:
    message = chat_service_mapping.to_message(_recording("Initial", link=False), "19:meeting@thread.v2")
    assert message.text == "[Recording: Weekly sync & review]"
    assert "asyncgw" not in message.text
    assert formatter.txt_message(message).splitlines()[0].endswith(" Teams")
