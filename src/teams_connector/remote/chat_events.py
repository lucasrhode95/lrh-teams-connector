"""Chat Service system-event records as one short line each.

Event records (``Event/Call``, ``ThreadActivity/*``) carry XML or JSON, not message HTML. Only what
Teams shows is kept: a meeting's subject, its duration and the names of its participants, the members
who joined or left, and a new topic. Identifiers, organizer addresses and recording flags are dropped.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any


def render(kind: str, content: str) -> str:
    """Entry point: the one-line text of one event record of type ``kind``.

    Assumes (not re-checked here): ``kind`` is the record's ``messagetype`` and ``content`` its raw
    content. A type without a known rendering, or content that does not parse, renders as ``[<kind>]``,
    never as the raw content.
    """
    if kind == "Event/Call":
        return _call(content) or f"[{kind}]"
    if kind == "ThreadActivity/MemberJoined":
        return _members(content, "joined") or f"[{kind}]"
    if kind == "ThreadActivity/MemberLeft":
        return _members(content, "left") or f"[{kind}]"
    if kind == "ThreadActivity/AddMember":
        return "[Member added]"
    if kind == "ThreadActivity/TopicUpdate":
        return _topic(content) or f"[{kind}]"
    if kind.casefold() == "richtext/media_calltranscript":
        return (
            "[Transcript saved]"  # the content is a JSON notice of identifiers and storage flags: never shown
        )
    return f"[{kind}]"


def _call(content: str) -> str | None:
    root = _xml(content)
    if root is None:
        return None
    if root.find("ended") is not None:
        return _ended(root)
    return _started(root)


def _started(root: ET.Element) -> str:
    subject = _text(root.findtext(".//subject"))
    return f"[Meeting started: {subject}]" if subject else "[Meeting started]"


def _ended(root: ET.Element) -> str:
    parts = root.findall("partlist/part")
    names = _unique(_text(part.findtext("displayName")) for part in parts)
    seconds = max((_seconds(part.findtext("duration")) for part in parts), default=0)
    fields = ["Meeting ended"]
    if seconds:
        fields.append(_duration(seconds))
    if names:
        fields.append("Participants: " + "; ".join(names))
    return "[" + " · ".join(fields) + "]"


def _members(content: str, verb: str) -> str | None:
    try:
        record = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("members"), list):
        return None
    names = _unique(
        _text(member.get("friendlyname")) for member in record["members"] if isinstance(member, dict)
    )
    return f"[{'; '.join(names)} {verb}]" if names else f"[A member {verb}]"


def _topic(content: str) -> str | None:
    root = _xml(content)
    if root is None:
        return None
    value = _text(root.findtext(".//value"))
    return f"[Topic changed to: {value}]" if value else "[Topic changed]"


def _xml(content: str) -> ET.Element | None:
    """The record's top-level elements sit side by side, so they are parsed under one wrapper."""
    try:
        return ET.fromstring(f"<root>{content}</root>")
    except ET.ParseError:
        return None


def _seconds(value: str | None) -> int:
    try:
        return max(int(value or 0), 0)
    except ValueError:
        return 0


def _duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, remainder = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {remainder:02d}s"


def _unique(values: Any) -> list[str]:
    names: list[str] = []
    for value in values:
        if value and value not in names:
            names.append(value)
    return names


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())
    return None
