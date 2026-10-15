"""Loop meeting-notes pages: the notes link in a bot message, and the page's HTML as plain text.

Synthetic fixtures only: the page, names and links are invented.
"""

from __future__ import annotations

from pathlib import Path

from teams_connector.remote.loop_notes import meeting_notes, notes_links

PAGE = (Path(__file__).parents[1] / "fixtures" / "loop_meeting_notes.html").read_text(encoding="utf-8")
LINK = "https://tenant-my.sharepoint.com/:fl:/g/personal/organizer/sample-page?e=abc"


def test_the_page_keeps_only_its_content() -> None:
    notes = meeting_notes(PAGE)

    assert notes.title == "Weekly sync 2026-10-14"
    assert notes.text == "\n".join(
        [
            "# Agenda",
            "Goal: agree on the release date for the sample app.",
            "- Review open bugs",
            "- Plan the demo",
            "",
            "# Meeting notes",
            "",
            "## Release",
            "The team agreed to ship on Friday.",
            "",
            "# Follow-up tasks",
            "- Draft the release notes (assigned to Alex Doe, due 10/17/2026)",
            "- Book the demo room",
        ]
    )


def test_a_table_without_tasks_keeps_its_rows() -> None:
    notes = meeting_notes(
        "<table><tr><th>Name</th><th>Role</th></tr><tr><td>Sam</td><td>Host</td></tr></table>"
    )

    assert notes.text == "Name | Role\nSam | Host"


def test_only_fluid_embeds_of_loop_files_are_notes_links() -> None:
    content = (
        f'<p>That is a wrap. <a href="{LINK.replace("&", "&amp;")}" '
        'itemtype="http://schema.skype.com/FluidAutoEmbedLink" itemid="1"></a>'
        '<a href="https://example.com/:fl:/x">ordinary link</a>'
        '<a href="https://tenant.sharepoint.com/:w:/doc" itemtype="http://schema.skype.com/FluidAutoEmbedLink">'
        "</a></p>"
    )

    assert notes_links(content) == [LINK]
