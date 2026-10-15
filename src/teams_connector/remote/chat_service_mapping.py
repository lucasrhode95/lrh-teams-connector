"""Teams Chat Service / CSA JSON → domain models. The only place that knows their message field names."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from teams_connector.domain.models import Attachment, Message
from teams_connector.remote import chat_events, inline_images, teams_html

_MRI = re.compile(
    r"(?i)(?<![a-z0-9])((?:8:orgid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})|"
    r"(?:28:[a-z0-9._-]+))(?![a-z0-9])"
)
_OBJECT_ID = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ORDINARY_TYPES = ("richtext", "text")  # message types that carry user content (prefixes)
_DELETE_TYPES = {"messagedelete"}


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def user_id(value: Any) -> str | None:
    """Canonical person id: the Entra object id for ``8:orgid:`` MRIs and bare GUIDs; other MRIs (apps
    ``28:...``) lower-cased as they are."""
    if not isinstance(value, str) or not value.strip():
        return None
    identifier = value.strip().lower()
    if identifier.startswith("8:orgid:"):
        identifier = identifier[len("8:orgid:") :]
    return identifier or None


def member_id(member: dict[str, Any]) -> str | None:
    for key in ("id", "mri", "objectId", "userId", "userMri"):
        identifier = user_id(member.get(key))
        if identifier:
            return identifier
    return None


def message_author_id(message: dict[str, Any]) -> str | None:
    """The sender's canonical id when the message exposes one (``from`` is often a contact URL)."""
    id_keys = ("id", "mri", "objectId", "userId", "userMri")
    sender_keys = (
        "from", "fromId", "fromMri", "sender", "senderId", "senderMri",
        "authorId", "authorMri", "userId", "userMri",
    )  # fmt: skip
    for key in sender_keys:
        value = message.get(key)
        candidates = [value.get(k) for k in id_keys] if isinstance(value, dict) else [value]
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            normalized = candidate.strip()
            match = _MRI.search(normalized)
            if match:
                return user_id(match.group(1))
            if _OBJECT_ID.fullmatch(normalized):
                return normalized.lower()
    return None


def message_datetime(message: dict[str, Any]) -> datetime | None:
    """The message's arrival time in UTC."""
    for key in (
        "OriginalArrivalTime",
        "originalArrivalTime",
        "originalarrivaltime",
        "composeTime",
        "composetime",
    ):
        parsed = parse_time(message.get(key))
        if parsed is not None:
            return parsed
    return None


def parse_time(value: Any) -> datetime | None:
    """ISO text, epoch seconds or epoch milliseconds (number or digit string) → aware UTC datetime."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, int | float) and not isinstance(value, bool):
        seconds = value / 1000 if abs(value) > 100_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            return parse_time(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def order_key(message: dict[str, Any]) -> tuple[int, int, datetime, str]:
    """Chronological order: sequence id when present, then arrival time, then id."""
    try:
        sequence, has_sequence = int(message.get("sequenceId")), 1  # type: ignore[arg-type]
    except (TypeError, ValueError):
        sequence, has_sequence = 0, 0
    timestamp = message_datetime(message) or datetime.min.replace(tzinfo=UTC)
    return (0 if has_sequence else 1, sequence, timestamp, str(message.get("id") or ""))


def properties(message: dict[str, Any]) -> dict[str, Any]:
    value = message.get("properties")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def to_message(raw: dict[str, Any], conversation_id: str) -> Message:
    """One Chat Service message record as a domain message (text rendered, markers included)."""
    kind = _first_string(raw, "messagetype", "messageType") or ""
    content_type = _first_string(raw, "contenttype", "contentType") or ""
    props = properties(raw)
    content = _first_string(raw, "content", "body", "text") or ""
    lowered = kind.casefold()
    deleted_at = parse_time(props.get("deletetime") or raw.get("deletetime"))
    is_deleted = lowered in _DELETE_TYPES or deleted_at is not None
    system_event = None
    inline: list[Attachment] = []
    if kind and not lowered.startswith(_ORDINARY_TYPES) and lowered not in _DELETE_TYPES:
        system_event = kind
        text = chat_events.render(kind, content)
    else:
        is_html = "html" in kind.lower() or "html" in content_type.lower()
        rendered = teams_html.render_message(content, is_html=is_html)
        inline, text = rendered.images, rendered.text
    files = _files(raw, props, {item.name.casefold() for item in inline})
    if system_event is None:
        text = "\n\n".join(part for part in (text, "\n".join(file_markers(files))) if part)
    sent_at = message_datetime(raw)
    author = _first_string(raw, "imdisplayname", "imDisplayName", "fromDisplayNameInToken", "displayName")
    message_id = raw.get("id")
    if message_id is None:  # never seen in practice; keep the record addressable and stable
        fingerprint = json.dumps([conversation_id, raw.get("sequenceId"), str(sent_at), author, text])
        message_id = "generated:" + hashlib.sha256(fingerprint.encode()).hexdigest()
    subject = props.get("subject")
    return Message(
        id=str(message_id),
        conversation_id=conversation_id,
        author=author.strip() if author and author.strip() else None,
        author_id=message_author_id(raw),
        sent_at=sent_at,
        text=text,
        system_event=system_event,
        subject=subject.strip() if isinstance(subject, str) and subject.strip() else None,
        edited_at=parse_time(props.get("edittime") or raw.get("edittime")),
        is_deleted=is_deleted,
        deleted_at=deleted_at,
        attachments=[*inline, *files],
    )


def file_markers(files: list[Attachment]) -> list[str]:
    """Marker lines for the message's SharePoint and OneDrive files, shown after its text."""
    return [f"[File: {item.name}]" for item in files]


def _files(raw: dict[str, Any], props: dict[str, Any], used: set[str]) -> list[Attachment]:
    """The SharePoint and OneDrive files a message shares, named by their own file names."""
    files = _as_items(props.get("files") or raw.get("files") or raw.get("attachments"))
    out: list[Attachment] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        info = mapping(item.get("fileInfo"))
        name = item.get("fileName") or item.get("filename") or item.get("name") or item.get("displayName")
        url = (
            info.get("shareUrl") or info.get("fileUrl") or item.get("fileUrl") or item.get("shareUrl")
            or item.get("objectUrl") or item.get("url")
        )  # fmt: skip
        file_type = str(item.get("fileType") or item.get("contentType") or "")
        link = url.strip() if isinstance(url, str) and url.strip() else None
        out.append(
            Attachment(
                name=inline_images.unique_name(str(name) if name else "file", used),
                kind="file",
                source="sharepoint" if link else None,
                url=link,
                content_type=file_type if "/" in file_type else None,
            )
        )
    return out


def _as_items(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if any(key in value for key in ("fileName", "filename", "name", "fileInfo")):
            return [value]
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def _first_string(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str):
            return value
    return None


def preview(content: Any) -> str | None:
    """A short readable preview of a catalog row's last message."""
    if not isinstance(content, str) or not content.strip():
        return None
    text = teams_html.render_body(content, is_html="<" in content)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240] or None
