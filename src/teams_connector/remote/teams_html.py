"""Teams rich text to readable text: HTML, mentions, SWIFT cards, Adaptive Cards, lists, links, code.

Both backends deliver Teams message bodies as Teams-flavoured HTML (Chat Service with mention spans and
URIObject/SWIFT payloads, Graph with ``<at>`` mentions and ``<attachment>`` placeholders). Everything here
is pure: no network, no Teams field names beyond the markup itself.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from html.parser import HTMLParser
from typing import Any

_URI_OBJECT_RE = re.compile(
    r"<URIObject\b(?P<attributes>[^>]*)>(?P<content>.*?)</URIObject\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<Swift\b[^>]*>", re.IGNORECASE)
_TAG_END_RE = re.compile(r"</Swift\s*>", re.IGNORECASE)


class _TagAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attributes: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.attributes = {name.lower(): value or "" for name, value in attrs}

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _tag_attributes(source: str, tag_name: str) -> dict[str, str]:
    parser = _TagAttributeParser()
    try:
        parser.feed(f"<{tag_name} {source}>")
        parser.close()
    except Exception:
        return {}
    return parser.attributes


def _remove_swift_payload_elements(source: str) -> tuple[str, list[str]]:
    """Remove every Swift b64 element, returning its encoded values for decoding."""
    output: list[str] = []
    payloads: list[str] = []
    cursor = 0
    for opening in _TAG_RE.finditer(source):
        tag_attributes = opening.group(0)[len("<Swift") : -1].strip()
        attrs = _tag_attributes(tag_attributes, "Swift")
        payload = attrs.get("b64")
        if payload is None:
            continue

        output.append(source[cursor : opening.start()])
        payloads.append(payload)
        end = opening.end()
        if not opening.group(0).rstrip().endswith("/>"):
            closing = _TAG_END_RE.search(source, end)
            if closing is not None:
                end = closing.end()
        cursor = end
    output.append(source[cursor:])
    return "".join(output), payloads


def _normalized_for_deduplication(value: str) -> str:
    without_markdown = re.sub(r"[*_`]+", "", value)
    return re.sub(r"[^\w]+", " ", without_markdown, flags=re.UNICODE).casefold().strip()


def _merge_uri_object_text(visible_text: str, card_text: list[str]) -> str:
    """Merge visible fallback and card text without repeating identical content."""
    output: list[str] = []
    seen: list[str] = []
    for value in [visible_text, *card_text]:
        for line in value.splitlines() or [value]:
            cleaned = re.sub(r"[\t ]+", " ", html.unescape(line)).strip()
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


def _adaptive_card_text(payload: Any) -> list[str]:
    """Extract user-facing text from Teams card envelopes and Adaptive Cards."""
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
        if node_type == "textblock":
            add(node.get("text"))
        elif node_type == "richtextblock":
            visit(node.get("inlines", []))
        elif node_type == "textrun":
            add(node.get("text"))
        elif node_type == "factset":
            facts = node.get("facts", [])
            if isinstance(facts, list):
                for fact in facts:
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

        # These are the card's content-bearing children. Other properties such
        # as spacing, color, width, alignment, and image URLs are presentation.
        for key in (
            "body",
            "items",
            "columns",
            "actions",
            "inlines",
            "images",
        ):
            child = node.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

    if not isinstance(payload, dict):
        return output

    add(payload.get("summary"))
    add(payload.get("text"))
    attachments = payload.get("attachments", [])
    if isinstance(attachments, list):
        for attachment in attachments:
            if isinstance(attachment, dict):
                content = attachment.get("content")
                if isinstance(content, dict):
                    visit(content)
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


def _render_visible_html(source: str) -> str:
    parser = TeamsHtmlParser()
    try:
        parser.feed(source)
        parser.close()
        return parser.text()
    except Exception:
        return html.unescape(re.sub(r"<[^>]*>", "", source)).strip()


def _rewrite_swift_uriobjects(source: str, *, as_html: bool) -> tuple[str, bool]:
    """Replace SWIFT.1 URIObjects with safe visible and decoded card text."""
    found = False

    def replace(match: re.Match[str]) -> str:
        nonlocal found
        outer_attrs = _tag_attributes(match.group("attributes"), "URIObject")
        inner = match.group("content")
        visible_source, encoded_payloads = _remove_swift_payload_elements(inner)
        if outer_attrs.get("type", "").casefold() != "swift.1":
            # Even if a non-card URIObject has a Swift field, never print its
            # encoded representation into an export.
            if not encoded_payloads:
                return match.group(0)
            found = True
            visible = _render_visible_html(visible_source)
            return html.escape(visible, quote=False) if as_html else visible

        found = True
        visible = _render_visible_html(visible_source)
        card_text: list[str] = []
        for encoded in encoded_payloads:
            compact = "".join(encoded.split())
            try:
                raw = base64.b64decode(compact, validate=True)
                decoded = json.loads(raw.decode("utf-8-sig"))
                card_text.extend(_adaptive_card_text(decoded))
            except (
                binascii.Error,
                UnicodeDecodeError,
                json.JSONDecodeError,
                RecursionError,
                ValueError,
            ):
                continue

        merged = _merge_uri_object_text(visible, card_text)
        return html.escape(merged, quote=False) if as_html else merged

    rewritten = _URI_OBJECT_RE.sub(replace, source)
    # Also strip an orphaned Swift payload element if the surrounding Teams
    # markup is malformed and did not match the complete URIObject wrapper.
    rewritten, orphan_payloads = _remove_swift_payload_elements(rewritten)
    return rewritten, found or bool(orphan_payloads)


class TeamsHtmlParser(HTMLParser):
    """Convert common Teams rich-text markup into readable Markdown-like text."""

    BLOCK_TAGS = {"p", "div", "section", "article", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_stack: list[dict[str, Any]] = []
        self.link_stack: list[tuple[int, str]] = []
        self.pre_depth = 0
        self.ignored_depth = 0
        self.suppress_leading_mention_at = False
        self.mention_tags: list[str] = []
        self.mention_content_starts: list[int] = []
        self.mention_join_snapshots: list[tuple[list[str], int] | None] = []
        self.last_mention_content_start: int | None = None
        self.last_mention_close_index: int | None = None
        self.paragraph_starts: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        item_type = attributes.get("itemtype", "").lower()
        if item_type.rstrip("/").endswith("/mention"):
            join_snapshot = self._join_previous_mention()
            self.mention_tags.append(tag)
            self.mention_join_snapshots.append(join_snapshot)
            self.suppress_leading_mention_at = True
            if join_snapshot is None:
                self.parts.append("@(")
            self.mention_content_starts.append(len(self.parts))
        if "amsimage" in item_type:
            image_url = (
                attributes.get("src")
                or attributes.get("data-src")
                or attributes.get("itemid")
                or attributes.get("alt")
            )
            if image_url or tag == "img":
                self._newline(1)
                self.parts.append(f"[Image: {image_url or 'inline image'}]")
                if tag == "img":
                    return
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag == "p":
            self.paragraph_starts.append(len(self.parts))
            self._newline(1)
        elif tag in self.BLOCK_TAGS:
            self._newline(2)
        elif tag == "br":
            self._newline(1)
        elif tag in {"ul", "ol"}:
            self.list_stack.append({"tag": tag, "next": self._positive_int(attributes.get("start"), 1)})
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
            href = attributes.get("href", "").strip()
            self.link_stack.append((len(self.parts), href))
        elif tag == "pre":
            self._newline(2)
            self.parts.append("```\n")
            self.pre_depth += 1
        elif tag == "code" and not self.pre_depth:
            self.parts.append("`")
        elif tag == "img":
            item_type = attributes.get("itemtype", "").lower()
            if "amsimage" in item_type or attributes.get("src"):
                image_url = (
                    attributes.get("src")
                    or attributes.get("data-src")
                    or attributes.get("itemid")
                    or attributes.get("alt")
                )
                self._newline(1)
                self.parts.append(f"[Image: {image_url or 'inline image'}]")

    def handle_endtag(self, tag: str) -> None:
        if self.mention_tags and tag == self.mention_tags[-1]:
            self.mention_tags.pop()
            content_start = self.mention_content_starts.pop()
            join_snapshot = self.mention_join_snapshots.pop()
            self.suppress_leading_mention_at = False
            if join_snapshot is not None:
                snapshot, previous_content_start = join_snapshot
                second_content = self.parts[content_start:]
                second_name = self._plain_mention_text(second_content)
                if not second_name or "," in second_name:
                    self.parts = snapshot + ["@(", *second_content, ")"]
                    self.last_mention_content_start = len(snapshot) + 1
                else:
                    self.parts.append(")")
                    self.last_mention_content_start = previous_content_start
            else:
                self.parts.append(")")
                self.last_mention_content_start = content_start
            self.last_mention_close_index = len(self.parts) - 1
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag == "p":
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
            if label and href and href != label:
                self.parts.append(f"{label} ({href})")
            else:
                self.parts.append(label or href)
        elif tag == "pre" and self.pre_depth:
            self.pre_depth -= 1
            self._newline(1)
            self.parts.append("\n```")
            self._newline(2)
        elif tag == "code" and not self.pre_depth:
            self.parts.append("`")

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        has_line_break = "\n" in data.replace("\r", "\n")
        if not self.pre_depth and not data.strip() and has_line_break and self._at_line_start():
            # Teams stores a line break between two paragraphs as whitespace; at a line start it is layout.
            return
        if not self.mention_tags and data.strip():
            self.last_mention_content_start = None
            self.last_mention_close_index = None
        if self.suppress_leading_mention_at and data.strip():
            leading_whitespace = len(data) - len(data.lstrip())
            if data[leading_whitespace:].startswith("@"):
                data = data[:leading_whitespace] + data[leading_whitespace + 1 :]
            self.suppress_leading_mention_at = False
        self.parts.append(data)

    def _join_previous_mention(self) -> tuple[list[str], int] | None:
        close_index = self.last_mention_close_index
        content_start = self.last_mention_content_start
        if self.mention_tags or close_index is None or content_start is None:
            return None
        if not self.parts[close_index].endswith(")"):
            return None

        first_name = self._plain_mention_text(self.parts[content_start:close_index])
        if not first_name.endswith(","):
            return None

        separator = "".join(self.parts[close_index + 1 :])
        if any(character not in " \t\r\n\xa0" for character in separator):
            return None

        snapshot = list(self.parts)
        del self.parts[close_index:]
        self.parts.append(" ")
        self.last_mention_close_index = None
        return snapshot, content_start

    @staticmethod
    def _plain_mention_text(parts: list[str]) -> str:
        text = "".join(parts).replace("\xa0", " ")
        text = re.sub(r"[*_`]+", "", text)
        return text.strip()

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

    @staticmethod
    def _positive_int(value: str | None, fallback: int) -> int:
        if value is None:
            return fallback
        try:
            parsed = int(value)
        except ValueError:
            return fallback
        return parsed if parsed > 0 else fallback

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


_AT_RE = re.compile(r"<at\b[^>]*>(?P<name>.*?)</at\s*>", re.IGNORECASE | re.DOTALL)
_ATTACHMENT_RE = re.compile(r"<attachment\b[^>]*>\s*</attachment\s*>|<attachment\b[^>]*/>", re.IGNORECASE)
_MENTION_SPAN = '<span itemtype="http://schema.skype.com/Mention">{name}</span>'


def adaptive_card_text(payload: Any) -> list[str]:
    """User-facing text of a Teams card envelope or Adaptive Card (deduplicated, in order)."""
    return _adaptive_card_text(payload)


def render_body(content: str, *, is_html: bool) -> str:
    """Readable text for one Teams message body (HTML or plain text)."""
    decoded_content = html.unescape(content)
    if not re.search(r"<URIObject\b", content, re.IGNORECASE) and re.search(
        r"<URIObject\b", decoded_content, re.IGNORECASE
    ):
        content = decoded_content
    content, _ = _rewrite_swift_uriobjects(content, as_html=is_html)
    if not content:
        return ""
    if not is_html:
        return normalize_plain_text(content)
    parser = TeamsHtmlParser()
    try:
        parser.feed(content)
        parser.close()
        return parser.text()
    except Exception:
        return _render_visible_html(content)


def graph_html(content: str) -> str:
    """Graph's message HTML in Chat Service form: ``<at>`` mentions become Teams mention spans, and the
    ``<attachment>`` placeholders (rendered separately) are removed."""
    content = _ATTACHMENT_RE.sub("", content)
    return _AT_RE.sub(lambda m: _MENTION_SPAN.format(name=m.group("name")), content)


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
