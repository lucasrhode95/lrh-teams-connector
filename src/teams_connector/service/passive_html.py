"""Passive HTML: the rules for HTML a Teams message is sent with.

The Outlook connector keeps the reference rule set (outlook_connector/service/passive_html.py); this is the
Teams copy. The HTML is checked once, where it enters the send, and then sent as written: nothing is parsed
into another form, and nothing is rewritten.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from teams_connector.domain.errors import InvalidRequest

ACTIVE_TAGS = frozenset(
    {"script", "iframe", "object", "embed", "form", "input", "button", "textarea", "select"}
)
# Elements a browser reads as text (or that never end, as plaintext does). This parser reads the markup
# after them as tags, so a fragment that ends after one would be checked as tags but rendered as text.
STATE_CHANGING_TAGS = frozenset({"plaintext", "title", "xmp", "noembed", "noframes", "noscript"})
_WHITESPACE = re.compile(r"[\s\x00-\x1f]+")


def check_passive_html(value: str) -> None:
    """Entry point for HTML a message is sent with: refuse active content, elements that change how the
    rest of the markup is read, and unfinished markup. The HTML itself is not changed.

    HTMLParser never reports an unfinished tag, quote, comment or script; it keeps them in ``rawdata``.
    The trailing space lets the parser flush text that ends in ``&`` (such as "Tom&Jerry") without hiding
    an unfinished construct.
    """
    parser = _StartTags()
    parser.feed(value + " ")
    for name, attributes in parser.tags:
        _refuse(name, attributes)
    if parser.rawdata:
        raise InvalidRequest("The HTML ends inside a tag, attribute, comment or script; complete it.")


def _refuse(name: str, attributes: list[tuple[str, str | None]]) -> None:
    if name in ACTIVE_TAGS:
        raise InvalidRequest(f"Active HTML content is not allowed: {name}.")
    if name in STATE_CHANGING_TAGS:
        raise InvalidRequest(f"HTML that changes how the rest of the markup is read is not allowed: {name}.")
    for attribute, value in attributes:
        compact = _WHITESPACE.sub("", unescape(value or "")).lower()
        if attribute.startswith("on") or compact.startswith(("javascript:", "vbscript:")):
            raise InvalidRequest("Active HTML URLs and event handlers are not allowed.")


class _StartTags(HTMLParser):
    """Collects the start tags the parser completes; ``rawdata`` keeps whatever it could not finish."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))

    handle_startendtag = handle_starttag
