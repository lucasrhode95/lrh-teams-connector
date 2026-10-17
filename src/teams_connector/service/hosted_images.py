"""Hosted images: the Teams rule for the ``<img>`` tags and embedded media a message is sent with.

Teams shows a pasted image only as an upload it hosts. An ``<img>`` is therefore allowed only when its
``src`` is exactly the file name of an image sent with the message; any other ``<img>``, and every
embedded-media tag, is refused rather than sent as a broken or external picture. An allowed ``<img>`` is
rebuilt to point at the hosted copy; nothing else in the HTML changes.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from html import escape

from teams_connector.domain.errors import InvalidRequest
from teams_connector.service.passive_html import passive_start_tags

# A browser reads <image> as <img>, and inline SVG can hold its own <image>.
UNSUPPORTED_TAGS = frozenset({"img", "image", "svg", "picture", "source", "video", "audio"})
# An <img> naming an image sent with the message keeps only these attributes; srcset and the like could
# still load a remote picture.
IMAGE_ATTRIBUTES = frozenset({"src", "alt", "title", "width", "height", "style"})


@dataclass(frozen=True)
class ImageTag:
    """An ``<img>`` naming an image sent with the message: where its start tag is in the HTML."""

    start: int
    end: int
    attributes: tuple[tuple[str, str | None], ...]

    @property
    def src(self) -> str:
        return next((value or "" for name, value in self.attributes if name == "src"), "")


def check_hosted_images(value: str, image_names: Collection[str] = ()) -> list[ImageTag]:
    """Entry point for HTML a Teams message is sent with: the shared passive rules, then the image rule.

    An ``<img>`` whose ``src`` is exactly one of ``image_names`` (images sent with the message) is allowed
    and returned, so it can be pointed at the hosted copy. Any other ``<img>`` or embedded-media tag is
    refused. The HTML itself is not changed.
    """
    images = []
    for tag in passive_start_tags(value):
        if tag.name == "img" and image_names and dict(tag.attributes).get("src") in image_names:
            _check_image_attributes(tag.attributes)
            start = _offset(value, tag.position)
            images.append(ImageTag(start=start, end=start + len(tag.source), attributes=tag.attributes))
        elif tag.name in UNSUPPORTED_TAGS:
            raise InvalidRequest(
                f"Embedded media is not sent from HTML ({tag.name}): Teams shows only images it hosts. "
                'Pass PNG or JPEG files in images and name each one in <img src="file name">, '
                "or send a link."
            )
    return images


def with_hosted_images(value: str, images: list[ImageTag], sources: Mapping[str, str]) -> str:
    """``value`` with each of ``images`` (from ``check_hosted_images``) rebuilt so its ``src`` is
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


def _check_image_attributes(attributes: tuple[tuple[str, str | None], ...]) -> None:
    names = [name for name, _ in attributes]
    if any(name not in IMAGE_ATTRIBUTES for name in names) or names.count("src") != 1:
        raise InvalidRequest(
            "An image tag takes one src (the image's file name) and only alt, title, width, height and style."
        )


def _offset(value: str, position: tuple[int, int]) -> int:
    """The string offset of HTMLParser's (line, column) position; lines end at "\n" only."""
    line, column = position
    offset = 0
    for _ in range(line - 1):
        offset = value.index("\n", offset) + 1
    return offset + column
