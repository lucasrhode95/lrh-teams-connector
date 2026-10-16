"""Loop meeting-notes pages: detecting the notes link in a chat message, and reading Graph's HTML conversion
of the page as plain text. Pure: no network.

The meeting's "Facilitator" bot posts the link as an empty ``<a>`` with the ``FluidAutoEmbedLink`` item type
and a ``/:fl:/`` (Loop file) path. Graph's ``content?format=html`` turns the page into HTML with ``h1``
sections (agenda and goal, notes, follow-up tasks) and one task table. Only that content is kept: the AI
disclaimer and its link, empty paragraphs, the task checkboxes and the "Task list category" labels are
dropped (docs/api-research.md, "Loop pages and meeting notes", 2026-10-15).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from teams_connector.remote.ports import MeetingNotes

FLUID_EMBED = "http://schema.skype.com/fluidautoembedlink"
_BOILERPLATE = re.compile(r"^AI-generated content\b.*\bmay be incorrect\b", re.IGNORECASE)
_TASK_CATEGORY = re.compile(r"Task list category\s*:\s*\S*", re.IGNORECASE)
_HEADINGS = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}
_BLOCKS = {"p", "div", "li", "section", "article", "header", "footer", "blockquote", "label", *_HEADINGS}


def notes_links(content: str) -> list[str]:
    """The Loop page links a message embeds (``FluidAutoEmbedLink`` anchors with a ``/:fl:/`` https link)."""
    parser = _Anchors()
    try:
        parser.feed(content)
        parser.close()
    except Exception:
        return []
    return [
        href
        for item_type, href in parser.anchors
        if item_type == FLUID_EMBED and href.startswith("https://") and "/:fl:/" in urlsplit(href).path
    ]


def meeting_notes(page_html: str) -> MeetingNotes:
    """The readable content of a Loop page converted to HTML: headings, paragraphs, lists, and each task
    with its assignee and due date."""
    parser = _NotesParser()
    parser.feed(page_html)
    parser.close()
    title = " ".join(parser.title.split()) or None
    return MeetingNotes(title=title, text="\n".join(parser.lines).strip())


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attributes = {name: (value or "").strip() for name, value in attrs}
            self.anchors.append((attributes.get("itemtype", "").lower(), attributes.get("href", "")))


class _NotesParser(HTMLParser):
    """Blocks as lines (a blank line before each heading), lists as "- " items, tables as tasks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.title = ""
        self._buffer: list[str] = []
        self._prefix = ""
        self._ignored = 0  # inside head, script or style
        self._in_title = False
        self._rows: list[list[str]] | None = None  # the table being read
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in {"head", "script", "style"}:
            self._ignored += 1
        elif tag == "table":
            self._flush()
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._rows.append([])
        elif tag in {"td", "th"} and self._rows is not None:
            self._cell = []
        elif tag == "br":
            self._data(" ")
        elif tag in _BLOCKS and self._cell is None:
            self._flush()
            self._prefix = _HEADINGS.get(tag, "- " if tag == "li" else "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in {"head", "script", "style"} and self._ignored:
            self._ignored -= 1
        elif tag in {"td", "th"} and self._cell is not None and self._rows:
            self._rows[-1].append(_clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "table" and self._rows is not None:
            self.lines.extend(_table_lines(self._rows))
            self._rows = None
        elif tag in _BLOCKS and self._cell is None:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._ignored:
            self._data(data)

    def _data(self, data: str) -> None:
        (self._cell if self._cell is not None else self._buffer).append(data)

    def _flush(self) -> None:
        text = _clean("".join(self._buffer))
        prefix, self._prefix, self._buffer = self._prefix, "", []
        if not text or _BOILERPLATE.match(text) or text.lower() == "learn more":
            return
        if prefix.startswith("#") and self.lines:
            self.lines.append("")
        self.lines.append(prefix + text)


def _table_lines(rows: list[list[str]]) -> list[str]:
    """A task table (a header with "Task") as one line per task with its assignee and due date; any other
    table as its rows, cells joined by " | "."""
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    header = [cell.casefold() for cell in rows[0]]
    if "task" not in header:
        return [" | ".join(cell for cell in row if cell) for row in rows]
    task, owner, due = (
        header.index(name) if name in header else None for name in ("task", "assigned to", "due date")
    )
    lines = []
    for row in rows[1:]:
        cells = dict(enumerate(row))
        name = cells.get(task, "") if task is not None else ""
        if not name:
            continue
        details = [
            f"{label} {value}"
            for label, index in (("assigned to", owner), ("due", due))
            if index is not None and (value := cells.get(index, ""))
        ]
        lines.append(f"- {name}" + (f" ({', '.join(details)})" if details else ""))
    return lines


def _clean(text: str) -> str:
    return " ".join(_TASK_CATEGORY.sub("", text.replace("\xa0", " ")).split())
