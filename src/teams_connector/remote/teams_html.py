"""Teams rich text to readable text: HTML, mentions, SWIFT cards, Adaptive Cards, lists, links, code, tables.

Both backends deliver Teams message bodies as Teams-flavoured HTML (Chat Service with mention spans and
URIObject/SWIFT payloads, Graph with ``<at>`` mentions and ``<attachment>`` placeholders). One parser pass
renders all of it. Everything here is pure: no network, no Teams field names beyond the markup itself.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from teams_connector.domain.models import Attachment
from teams_connector.remote import inline_images

_URI_OBJECT_RE = re.compile(r"<URIObject\b", re.IGNORECASE)
_AT_RE = re.compile(r"<at\b(?P<attributes>[^>]*)>(?P<name>.*?)</at\s*>", re.IGNORECASE | re.DOTALL)
_AT_ID_RE = re.compile(r"""\bid\s*=\s*["']?(?P<id>[^"'\s>]+)""", re.IGNORECASE)
_ATTACHMENT_RE = re.compile(r"<attachment\b[^>]*>\s*</attachment\s*>|<attachment\b[^>]*/>", re.IGNORECASE)
_MENTION_SPAN = '<span itemtype="http://schema.skype.com/Mention" itemid="{id}">{name}</span>'
_TABLE_TAGS = {"table", "tr", "td", "th"}


@dataclass(frozen=True)
class RenderedBody:
    """A message body's readable text, and its inline images as attachments, named as the text names them."""

    text: str
    images: list[Attachment]


def render_message(
    content: str, *, is_html: bool, mention_people: Mapping[str, str] | None = None
) -> RenderedBody:
    """Readable text for one Teams message body (HTML or plain text), with its inline images.

    ``mention_people`` maps a mention element's ``itemid`` to the person it names, from the message's own
    mention list. Teams splits a ``Last, First`` name into one element per part; adjacent elements naming
    the same person read as one ``@(Last, First)`` mention. A plain-text body holding a card (URIObject) is
    parsed as markup so the card's payload never shows.
    """
    if not _URI_OBJECT_RE.search(content) and _URI_OBJECT_RE.search(html.unescape(content)):
        content = html.unescape(content)  # a card stored escaped
    if not content:
        return RenderedBody("", [])
    if not is_html and not _URI_OBJECT_RE.search(content):
        return RenderedBody(normalize_plain_text(content), [])
    parser = TeamsHtmlParser(mention_people or {})
    try:
        parser.feed(content)
        parser.close()
    except AssertionError:  # Python 3.12 rejects some marked sections ("<![x[")
        return RenderedBody(normalize_plain_text(html.unescape(re.sub(r"<[^>]*>", " ", content))), [])
    return RenderedBody(parser.text(), parser.images)


def graph_html(content: str) -> str:
    """Graph's message HTML in Chat Service form: each ``<at id="n">`` mention becomes a Teams mention span
    with ``itemid="n"``, and the ``<attachment>`` placeholders (rendered separately) are removed."""
    content = _ATTACHMENT_RE.sub("", content)

    def span(match: re.Match[str]) -> str:
        identifier = _AT_ID_RE.search(match.group("attributes"))
        item = html.escape(identifier.group("id"), quote=True) if identifier else ""
        return _MENTION_SPAN.format(id=item, name=match.group("name"))

    return _AT_RE.sub(span, content)


def adaptive_card_text(payload: Any) -> list[str]:
    """User-facing text of a Teams card envelope or Adaptive Card (deduplicated, in order)."""
    output: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                output.append(text)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        node_type = str(node.get("type", "")).casefold()
        if node_type in ("textblock", "textrun"):
            add(node.get("text"))
        elif node_type == "factset":
            for fact in node.get("facts") or []:
                if not isinstance(fact, dict):
                    continue
                title, value = fact.get("title"), fact.get("value")
                if title is not None and value is not None:
                    add(f"{title}: {value}")
                else:
                    add(title if title is not None else value)
        elif node_type == "image":
            add(node.get("altText"))
        elif node_type.startswith("action."):
            add(node.get("title") or node.get("label"))
            add(node.get("url"))
        elif node_type.startswith("input."):
            add(node.get("label"))
            if node.get("id"):
                add(f"Field ID: {node['id']}")
            add(node.get("value"))
            add(node.get("placeholder"))
        # The card's content-bearing children; other properties (spacing, colour, image URLs) are layout.
        for key in ("body", "items", "columns", "actions", "inlines", "images"):
            child = node.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

    if not isinstance(payload, dict):
        return output
    add(payload.get("summary"))
    add(payload.get("text"))
    for attachment in payload.get("attachments") or []:
        if isinstance(attachment, dict) and isinstance(attachment.get("content"), dict):
            visit(attachment["content"])
    if str(payload.get("type", "")).casefold() == "adaptivecard":
        visit(payload)
    # A repeated block can occur in both the Teams summary and card body.
    deduplicated: list[str] = []
    seen: set[str] = set()
    for value in output:
        key = _normalized_for_deduplication(value)
        if key and key not in seen:
            deduplicated.append(value)
            seen.add(key)
    return deduplicated


def normalize_plain_text(content: str) -> str:
    lines = [re.sub(r"[\t ]+", " ", line).strip() for line in content.splitlines()]
    output: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if output and not blank:
                output.append("")
            blank = True
            continue
        output.append(line)
        blank = False
    while output and not output[-1]:
        output.pop()
    return "\n".join(output)


@dataclass
class _Card:
    """An open URIObject: where its visible text starts, its type and the SWIFT payloads it carries."""

    start: int
    kind: str
    payloads: list[str] = field(default_factory=list)


@dataclass
class _Mention:
    tag: str
    person: str | None
    content_start: int


class TeamsHtmlParser(HTMLParser):
    """Convert Teams rich-text markup into readable Markdown-like text.

    - A mention reads ``@(Name)``; adjacent mention elements naming the same person are one mention.
    - A SWIFT card (URIObject) reads as its visible fallback text merged with its decoded card text; the
      encoded payload never shows, in any URIObject or on its own.
    - A table becomes one ``| cell | cell |`` line per row; a row of ``<th>`` cells is followed by a
      ``| --- |`` separator. Line breaks inside a cell become spaces, and a table nested in a cell is
      flattened into that cell's text.
    """

    BLOCK_TAGS = {"p", "div", "section", "article"}

    def __init__(self, mention_people: Mapping[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[Attachment] = []
        self.image_names: set[str] = set()
        self.mention_people = mention_people
        self.list_stack: list[dict[str, Any]] = []
        self.link_stack: list[tuple[int, str]] = []
        self.paragraph_starts: list[int] = []
        self.pre_depth = 0
        self.ignored_depth = 0
        self.mentions: list[_Mention] = []
        # The last closed mention, while only whitespace follows it: its person and its ")" position.
        self.previous_mention: tuple[str, int] | None = None
        self.cards: list[_Card] = []
        self.cell_starts: list[int] = []  # open cells, outermost first: where each one's text begins
        self.row_cells = 0
        self.row_is_header = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        if attributes.get("itemtype", "").lower().rstrip("/").endswith("/mention"):
            self._start_mention(tag, attributes.get("itemid", ""))
        if tag == "swift":  # a card payload: kept for decoding, never shown
            if self.cards and attributes.get("b64") is not None:
                self.cards[-1].payloads.append(attributes["b64"])
            self.ignored_depth += 1
            return
        if tag == "img" and not self.ignored_depth:
            self._image(attributes)
            return
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag == "uriobject":
            self._newline(1)
            self.cards.append(_Card(len(self.parts), attributes.get("type", "").casefold()))
        elif tag in _TABLE_TAGS:
            self._start_table_part(tag)
        elif tag == "p":
            self.paragraph_starts.append(len(self.parts))
            self._newline(1)
        elif tag in self.BLOCK_TAGS:
            self._newline(2)
        elif tag == "br":
            self.parts.append("\n")  # hard break: two in a row, or one after a paragraph, leave a blank line
        elif tag in {"ul", "ol"}:
            self.list_stack.append({"tag": tag, "next": _positive_int(attributes.get("start"), 1)})
        elif tag == "li":
            self._newline(1)
            depth = max(0, len(self.list_stack) - 1)
            context = self.list_stack[-1] if self.list_stack else {"tag": "ul", "next": 1}
            if context["tag"] == "ol":
                marker = f"{context['next']}. "
                context["next"] += 1
            else:
                marker = "- "
            self.parts.append("  " * depth + marker)
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "u":
            self.parts.append("__")
        elif tag == "blockquote":
            self._newline(2)
            self.parts.append("> ")
        elif tag == "a":
            self.link_stack.append((len(self.parts), attributes.get("href", "").strip()))
        elif tag == "pre":
            self._newline(2)
            self.parts.append("```\n")
            self.pre_depth += 1
        elif tag == "code" and not self.pre_depth:
            self.parts.append("`")

    def handle_endtag(self, tag: str) -> None:
        if self.mentions and tag == self.mentions[-1].tag:
            self._end_mention()
        if tag in {"script", "style", "swift"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag == "uriobject" and self.cards:
            self._end_card(self.cards.pop())
        elif tag in _TABLE_TAGS:
            self._end_table_part(tag)
        elif tag == "p":
            self._end_paragraph()
        elif tag in self.BLOCK_TAGS or tag == "li":
            self._newline(2 if tag in self.BLOCK_TAGS else 1)
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._newline(1)
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "u":
            self.parts.append("__")
        elif tag == "blockquote":
            self._newline(2)
        elif tag == "a" and self.link_stack:
            start, href = self.link_stack.pop()
            label = "".join(self.parts[start:]).strip()
            del self.parts[start:]
            self.parts.append(f"{label} ({href})" if label and href and href != label else label or href)
        elif tag == "pre" and self.pre_depth:
            self.pre_depth -= 1
            self._newline(1)
            self.parts.append("```")
            self._newline(2)
        elif tag == "code" and not self.pre_depth:
            self.parts.append("`")

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        if (
            not self.pre_depth
            and not data.strip()
            and "\n" in data.replace("\r", "\n")
            and self._at_line_start()
        ):
            return  # Teams stores a break between paragraphs as whitespace; at a line start it is layout
        if data.strip():
            self.previous_mention = None
            mention = self.mentions[-1] if self.mentions else None
            if mention is not None and len(self.parts) == mention.content_start:
                data = data.lstrip().removeprefix("@")  # Chat Service may keep the "@" inside the element
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts).replace("\r", "").replace("\xa0", " ")
        result: list[str] = []
        in_code = False
        previous_blank = False
        for line in raw.split("\n"):
            if line.strip().startswith("```"):
                in_code = not in_code
                result.append(line.strip())
                previous_blank = False
                continue
            if in_code:
                result.append(line.rstrip())
                continue
            cleaned = re.sub(r"[\t ]+", " ", line).strip()
            if not cleaned:
                if result and not previous_blank:
                    result.append("")
                previous_blank = True
                continue
            result.append(cleaned)
            previous_blank = False
        while result and not result[-1]:
            result.pop()
        return "\n".join(result).strip()

    def _start_mention(self, tag: str, item_id: str) -> None:
        person = self.mention_people.get(item_id)
        previous = self.previous_mention
        if person is not None and previous is not None and previous[0] == person:
            del self.parts[previous[1] :]  # reopen the previous mention: drop its ")" and the whitespace
            self.parts.append(" ")
        else:
            self.parts.append("@(")
        self.mentions.append(_Mention(tag, person, len(self.parts)))
        self.previous_mention = None

    def _end_mention(self) -> None:
        mention = self.mentions.pop()
        self.parts.append(")")
        self.previous_mention = (mention.person, len(self.parts) - 1) if mention.person else None

    def _end_card(self, card: _Card) -> None:
        """A SWIFT card reads as its visible text and its decoded card text, each line once."""
        if card.kind != "swift.1":
            return  # any other URIObject keeps its visible text (its payloads were already left out)
        visible = "".join(self.parts[card.start :])
        del self.parts[card.start :]
        card_text: list[str] = []
        for encoded in card.payloads:
            try:
                decoded = json.loads(
                    base64.b64decode("".join(encoded.split()), validate=True).decode("utf-8-sig")
                )
            except (binascii.Error, UnicodeDecodeError, ValueError, RecursionError):
                continue
            card_text.extend(adaptive_card_text(decoded))
        self.parts.append(_merged_lines(visible, card_text))
        self._newline(1)

    def _start_table_part(self, tag: str) -> None:
        if self.cell_starts:  # a table inside a cell is flattened into that cell's text
            if tag in {"td", "th"}:
                self.cell_starts.append(len(self.parts))
            else:
                self.parts.append(" ")
            return
        if tag == "table":
            self._newline(2)
        elif tag == "tr":
            self._newline(1)
            self.parts.append("|")
            self.row_cells, self.row_is_header = 0, False
        else:
            self.cell_starts.append(len(self.parts))
            self.row_is_header = self.row_is_header or tag == "th"

    def _end_table_part(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell_starts:
            start = self.cell_starts.pop()
            cell = " ".join("".join(self.parts[start:]).split())
            del self.parts[start:]
            self.previous_mention = None  # positions recorded inside the cell no longer exist
            if self.cell_starts:
                self.parts.append(f" {cell} ")
            else:
                self.parts.append(f" {cell.replace('|', chr(92) + '|')} |")
                self.row_cells += 1
        elif self.cell_starts:
            self.parts.append(" ")
        elif tag == "tr":
            if self.row_is_header and self.row_cells:
                self._newline(1)
                self.parts.append("|" + " --- |" * self.row_cells)
                self.row_is_header = False
            self._newline(1)
        elif tag == "table":
            self._newline(2)

    def _image(self, attributes: dict[str, str]) -> None:
        image = inline_images.inline_image(attributes)
        if image.name is None:
            if image.own_line:
                self._newline(1)
            self.parts.append(image.text)
            return
        name = inline_images.unique_name(image.name, self.image_names)
        self._newline(1)
        self.parts.append(f"[Image: {name}]")
        if image.attachment is not None:
            self.images.append(image.attachment.model_copy(update={"name": name}))

    def _end_paragraph(self) -> None:
        """Consecutive paragraphs read as consecutive lines, as Teams shows them. An empty paragraph
        (no text, or only a line break) keeps its blank line."""
        start = self.paragraph_starts.pop() if self.paragraph_starts else len(self.parts)
        self._newline(1)
        if not "".join(self.parts[start:]).strip():
            self._newline(2)

    def _at_line_start(self) -> bool:
        current = "".join(self.parts)
        return not current or current.endswith("\n")

    def _newline(self, count: int) -> None:
        current = "".join(self.parts)
        trailing = len(current) - len(current.rstrip("\n"))
        if trailing < count:
            self.parts.append("\n" * (count - trailing))


def _merged_lines(visible: str, card_text: list[str]) -> str:
    """Visible fallback and card text as lines, without repeating a line another one already contains."""
    output: list[str] = []
    seen: list[str] = []
    for value in [visible, *card_text]:
        for line in value.splitlines() or [value]:
            cleaned = re.sub(r"[\t ]+", " ", line).strip()
            normalized = _normalized_for_deduplication(cleaned)
            if not normalized:
                continue
            if any(
                normalized == previous or (len(normalized) >= 3 and f" {normalized} " in f" {previous} ")
                for previous in seen
            ):
                continue
            output.append(cleaned)
            seen.append(normalized)
    return "\n".join(output)


def _normalized_for_deduplication(value: str) -> str:
    without_markdown = re.sub(r"[*_`]+", "", value)
    return re.sub(r"[^\w]+", " ", without_markdown, flags=re.UNICODE).casefold().strip()


def _positive_int(value: str | None, fallback: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback
