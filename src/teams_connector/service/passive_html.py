"""Passive HTML: the rules for HTML a Teams message is sent with.

The Outlook connector keeps the reference rule set (outlook_connector/service/passive_html.py); this is the
Teams copy. The HTML is checked once, where it enters the send, and then sent as written: nothing is parsed
into another form. The one rewrite is an ``<img>`` that names an image sent with the message: its tag is
rebuilt to point at the hosted copy (``with_hosted_images``).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from html import escape, unescape
from html.parser import HTMLParser

from teams_connector.domain.errors import InvalidRequest

ACTIVE_TAGS = frozenset(
    {"script", "iframe", "object", "embed", "form", "input", "button", "textarea", "select"}
)
# Elements a browser reads as text (or that never end, as plaintext does). This parser reads the markup
# after them as tags, so a fragment that ends after one would be checked as tags but rendered as text.
STATE_CHANGING_TAGS = frozenset({"plaintext", "title", "xmp", "noembed", "noframes", "noscript"})
# Teams shows a pasted image only as an upload it hosts; an <img> pointing elsewhere is not how Teams embeds
# images, so it is refused rather than sent as a broken or external picture. A browser reads <image> as
# <img>, and inline SVG can hold its own <image>.
UNSUPPORTED_TAGS = frozenset({"img", "image", "svg", "picture", "source", "video", "audio"})
# An <img> naming an image sent with the message keeps only these attributes; srcset and the like could
# still load a remote picture.
IMAGE_ATTRIBUTES = frozenset({"src", "alt", "title", "width", "height", "style"})
_WHITESPACE = re.compile(r"[\s\x00-\x1f]+")


@dataclass(frozen=True)
class ImageTag:
    """An ``<img>`` naming an image sent with the message: where its start tag is in the HTML."""

    start: int
    end: int
    attributes: list[tuple[str, str | None]]

    @property
    def src(self) -> str:
        return next((value or "" for name, value in self.attributes if name == "src"), "")


def check_passive_html(value: str, image_names: Collection[str] = ()) -> list[ImageTag]:
    """Entry point for HTML a message is sent with: refuse active content, embedded media, elements that
    change how the rest of the markup is read, declarations, and unfinished markup. An ``<img>`` whose
    ``src`` is exactly one of ``image_names`` (images sent with the message) is allowed, and returned so it
    can be pointed at the hosted copy; any other ``<img>`` is refused. The HTML itself is not changed.

    HTMLParser never reports an unfinished tag, quote, comment or script; it keeps them in ``rawdata``.
    The trailing space lets the parser flush text that ends in ``&`` (such as "Tom&Jerry") without hiding
    an unfinished construct.
    """
    parser = _StartTags()
    parser.feed(value + " ")
    if parser.declarations:
        # The parser swallows a declaration whole, while a browser ends an unknown one at its first ">":
        # markup inside it would be checked as text but rendered as tags.
        raise InvalidRequest("HTML declarations, CDATA sections and processing instructions are not allowed.")
    images = []
    for name, attributes, position, text in parser.tags:
        if name == "img" and image_names and dict(attributes).get("src") in image_names:
            _check_image_attributes(attributes)
            start = _offset(value, position)
            images.append(ImageTag(start=start, end=start + len(text), attributes=attributes))
            continue
        _refuse(name, attributes)
    if parser.rawdata:
        raise InvalidRequest("The HTML ends inside a tag, attribute, comment or script; complete it.")
    return images


def with_hosted_images(value: str, images: list[ImageTag], sources: Mapping[str, str]) -> str:
    """``value`` with each of ``images`` (from ``check_passive_html``) rebuilt so its ``src`` is
    ``sources[src]``; every other character is kept as written."""
    parts, cursor = [], 0
    for image in images:
        attributes = "".join(
            f' {name}="{escape(sources[image.src] if name == "src" else (text or ""))}"'
            for name, text in image.attributes
        )
        parts += [value[cursor : image.start], f"<img{attributes}>"]
        cursor = image.end
    return "".join(parts) + value[cursor:]


def _check_image_attributes(attributes: list[tuple[str, str | None]]) -> None:
    names = [name for name, _ in attributes]
    if any(name not in IMAGE_ATTRIBUTES for name in names) or names.count("src") != 1:
        raise InvalidRequest(
            "An image tag takes one src (the image's file name) and only alt, title, width, height and style."
        )
    for _, value in attributes:
        if _WHITESPACE.sub("", unescape(value or "")).lower().startswith(("javascript:", "vbscript:")):
            raise InvalidRequest("Active HTML URLs and event handlers are not allowed.")


def _offset(value: str, position: tuple[int, int]) -> int:
    """The string offset of HTMLParser's (line, column) position; lines end at "\n" only."""
    line, column = position
    offset = 0
    for _ in range(line - 1):
        offset = value.index("\n", offset) + 1
    return offset + column


def _refuse(name: str, attributes: list[tuple[str, str | None]]) -> None:
    if name in ACTIVE_TAGS:
        raise InvalidRequest(f"Active HTML content is not allowed: {name}.")
    if name in STATE_CHANGING_TAGS:
        raise InvalidRequest(f"HTML that changes how the rest of the markup is read is not allowed: {name}.")
    if name in UNSUPPORTED_TAGS:
        raise InvalidRequest(
            f"Embedded media is not sent from HTML ({name}): Teams shows only images it hosts. Pass PNG or "
            'JPEG files in images and name each one in <img src="file name">, or send a link.'
        )
    for attribute, value in attributes:
        compact = _WHITESPACE.sub("", unescape(value or "")).lower()
        if attribute.startswith("on") or compact.startswith(("javascript:", "vbscript:")):
            raise InvalidRequest("Active HTML URLs and event handlers are not allowed.")


class _StartTags(HTMLParser):
    """Collects the start tags the parser completes; ``rawdata`` keeps whatever it could not finish."""

    def __init__(self) -> None:
        super().__init__()
        # (name, attributes, (line, column) of the tag, the tag as written)
        self.tags: list[tuple[str, list[tuple[str, str | None]], tuple[int, int], str]] = []
        self.declarations = 0  # doctype, CDATA and other <!...> declarations, and <?...> instructions

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs, self.getpos(), self.get_starttag_text() or ""))

    handle_startendtag = handle_starttag

    def handle_decl(self, decl: str) -> None:
        self.declarations += 1

    def unknown_decl(self, data: str) -> None:
        self.declarations += 1

    def handle_pi(self, data: str) -> None:
        self.declarations += 1
