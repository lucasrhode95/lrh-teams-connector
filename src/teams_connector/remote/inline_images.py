"""Inline images in Teams message HTML: the marker each one shows, and the attachment that can fetch it.

Names come from the message alone, so one message always gives the same names. An AMS image is named by its
AMS object id and its file type (``itemscope``); a message that does not give one uses ``png``, the type AMS
names pasted images with. A ``data:`` image is named by the first eight hex digits of the SHA-256 of its
decoded bytes and its MIME type. Graph's copy of a channel image carries the AMS object id of the image it
copies, so it takes that name too, with ``png`` as its extension. Emoji and stickers are text, and a GIF is a
link. Nothing here fetches or writes anything.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from teams_connector.domain.models import Attachment

AMS_OBJECT_ID = re.compile(r"0-[a-z0-9]+-d[0-9]+-[0-9a-f]{32}")
AMS_OBJECT_PATH = re.compile(r"/v1/objects/([^/?#]+)")
# Graph's copy of a pasted image: a channel post's or reply's hosted content, and nothing else on Graph.
HOSTED_CONTENT_URL = re.compile(
    r"https://graph\.microsoft\.com/(?:v1\.0|beta)/teams/[^/?#]+/channels/[^/?#]+/messages/[^/?#]+"
    r"(?:/replies/[^/?#]+)?/hostedContents/([^/?#]+)/\$value"
)
FILE_TYPE = re.compile(r"[a-z0-9]{1,8}")
DATA_URI = re.compile(r"data:(image/[a-z0-9.+-]+);base64,(.*)", re.DOTALL | re.IGNORECASE)
EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}
PASTED_IMAGE_TYPE = "png"
AMS_HOST = "asm.skype.com"
INLINE_IMAGE_TEXT = "[Image: inline image]"


@dataclass(frozen=True)
class InlineImage:
    """One ``img`` element. An image with a marker has ``name``; any other element shows ``text`` only."""

    text: str
    name: str | None
    attachment: Attachment | None
    own_line: bool


def inline_image(attributes: Mapping[str, str]) -> InlineImage:
    """Classify one ``img`` element. ``attributes`` holds its attribute values, names lower-cased."""
    item_type = attributes.get("itemtype", "").casefold()
    src = attributes.get("src", "").strip()
    alt = attributes.get("alt", "").strip()
    if "/emoji" in item_type or "/sticker" in item_type:
        return InlineImage(alt or attributes.get("title", "").strip(), None, None, own_line=False)
    if item_type.endswith("/giphy") or _host(src).endswith(".giphy.com"):
        label = f"[GIF: {alt}]" if alt else "[GIF]"
        return InlineImage(f"{label} {src}".strip(), None, None, own_line=True)
    if src.casefold().startswith("data:"):
        return _data_image(src)
    if "hostedcontents" in src.casefold():
        return _hosted_image(attributes, src)  # downloadable only on the hosted-content route
    object_id = _ams_object_id(attributes, src)
    if object_id is not None:
        return _ams_image(object_id, attributes, src)
    return InlineImage(f"[Image: {src}]" if src else INLINE_IMAGE_TEXT, None, None, own_line=True)


def unique_name(name: str, used: set[str]) -> str:
    """``name``, or ``stem (2).ext``, ``stem (3).ext``... for the first free one. ``used`` holds the
    casefolded names taken so far and gains the result."""
    stem, extension = os.path.splitext(name)
    candidate, counter = name, 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({counter}){extension}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def _data_image(src: str) -> InlineImage:
    match = DATA_URI.fullmatch(src)
    if match is None:
        return InlineImage(INLINE_IMAGE_TEXT, None, None, own_line=True)
    try:
        payload = base64.b64decode("".join(match.group(2).split()), validate=True)
    except binascii.Error:
        return InlineImage(INLINE_IMAGE_TEXT, None, None, own_line=True)
    mime = match.group(1).casefold()
    digest = hashlib.sha256(payload).hexdigest()
    name = f"sha-{digest[:8]}.{EXTENSIONS.get(mime, 'bin')}"
    attachment = Attachment(
        name=name, kind="image", source="data", source_id=digest, content_type=mime, size=len(payload),
        payload=payload,
    )  # fmt: skip
    return InlineImage("", name, attachment, own_line=True)


def _hosted_image(attributes: Mapping[str, str], src: str) -> InlineImage:
    item_id = attributes.get("itemid", "").strip()
    if AMS_OBJECT_ID.fullmatch(item_id):
        source_id = item_id
        stem = item_id
    else:
        hosted = _hosted_content(src)
        source_id = hashlib.sha256((hosted.group(1) if hosted else src).encode()).hexdigest()
        stem = f"hosted-{source_id[:8]}"
    name = f"{stem}.{PASTED_IMAGE_TYPE}"
    url = src if _hosted_content(src) else None
    attachment = Attachment(
        name=name, kind="image", source="graph" if url else None, url=url, source_id=source_id
    )
    return InlineImage("", name, attachment, own_line=True)


def _hosted_content(src: str) -> re.Match[str] | None:
    """The hosted-content route match, or None for any other URL (dot segments included)."""
    if "/." in src or "%2e" in src.casefold():
        return None
    return HOSTED_CONTENT_URL.fullmatch(src)


def _ams_object_id(attributes: Mapping[str, str], src: str) -> str | None:
    """The AMS object id of an AMS image element, or None for any other image."""
    if "/amsimage" not in attributes.get("itemtype", "").casefold() and not _is_ams_host(_host(src)):
        return None
    item_id = attributes.get("itemid", "").strip()
    if AMS_OBJECT_ID.fullmatch(item_id):
        return item_id
    path = AMS_OBJECT_PATH.search(src)
    if path and AMS_OBJECT_ID.fullmatch(path.group(1)):
        return path.group(1)
    return None


def _ams_image(object_id: str, attributes: Mapping[str, str], src: str) -> InlineImage:
    scope = attributes.get("itemscope", "").strip().casefold()
    extension = scope if FILE_TYPE.fullmatch(scope) else PASTED_IMAGE_TYPE
    name = f"{object_id}.{extension}"
    url = _full_size_url(src, object_id)
    attachment = Attachment(
        name=name, kind="image", source="ams" if url else None, url=url, source_id=object_id
    )
    return InlineImage("", name, attachment, own_line=True)


def _full_size_url(src: str, object_id: str) -> str | None:
    """The full-size view on the AMS host the image was posted from; None when that host is not AMS."""
    parts = _split(src)
    if parts is None or parts.scheme != "https" or not _is_ams_host(parts.hostname or ""):
        return None
    return f"https://{parts.hostname}/v1/objects/{object_id}/views/imgpsh_fullsize"


def _is_ams_host(host: str) -> bool:
    return host == AMS_HOST or host.endswith("." + AMS_HOST)


def _host(url: str) -> str:
    parts = _split(url)
    return (parts.hostname or "") if parts else ""


def _split(url: str) -> SplitResult | None:
    try:
        return urlsplit(url)
    except ValueError:
        return None
