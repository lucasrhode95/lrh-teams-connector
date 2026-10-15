"""Microsoft Graph Teams JSON → domain models. The only file that knows Graph's Teams field names.

Unknown fields are ignored; missing optional fields become None.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from teams_connector.domain.models import Attachment, Chat, Message, Participant
from teams_connector.remote import teams_html
from teams_connector.remote.chat_service_mapping import file_markers, mapping, parse_time, shared_file
from teams_connector.remote.csa_mapping import channel_name

_CARD = "application/vnd.microsoft.card."


def channel(team: dict[str, Any], raw: dict[str, Any]) -> Chat:
    team_name = str(team.get("displayName") or "").strip()
    channel_label = str(raw.get("displayName") or raw.get("id") or "").strip()
    return Chat(
        id=str(raw["id"]),
        name=channel_name(team_name, channel_label) if team_name else channel_label,
        kind="channel",
        team_id=str(team["id"]),
    )


def participant(member: dict[str, Any]) -> Participant:
    identifier = member.get("userId")
    name = member.get("displayName")
    return Participant(
        id=identifier.lower() if isinstance(identifier, str) and identifier else None,
        name=name.strip() if isinstance(name, str) and name.strip() else None,
    )


def created(raw: dict[str, Any]) -> datetime | None:
    return parse_time(raw.get("createdDateTime"))


def to_message(raw: dict[str, Any], conversation_id: str) -> Message:
    sender = mapping(raw.get("from"))
    user = mapping(sender.get("user"))
    app = mapping(sender.get("application"))
    author = user.get("displayName") or app.get("displayName")
    author_id = None
    if isinstance(user.get("id"), str) and user["id"]:
        author_id = user["id"].lower()
    elif isinstance(app.get("id"), str) and app["id"]:
        author_id = f"28:{app['id'].lower()}"

    body = mapping(raw.get("body"))
    raw_content = body.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    is_html = str(body.get("contentType", "")).lower() == "html"
    rendered = teams_html.render_message(
        teams_html.graph_html(content) if is_html else content, is_html=is_html
    )
    text = rendered.text
    attachments: list[Attachment] = list(rendered.images)
    used = {image.name.casefold() for image in attachments}
    files: list[Attachment] = []
    card_text: list[str] = []
    for item in raw.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        content_type = str(item.get("contentType") or "")
        if content_type == "reference":
            name = str(item.get("name") or "file")
            url = item.get("contentUrl")
            link = url if isinstance(url, str) and url else None
            files.append(shared_file(name, link, used))
        elif content_type.startswith(_CARD):
            card = item.get("content")
            if isinstance(card, str):
                try:
                    card = json.loads(card)
                except json.JSONDecodeError:
                    card = None
            card_text.extend(teams_html.adaptive_card_text(card))
    attachments.extend(files)
    parts = [text, "\n".join(card_text), "\n".join(file_markers(files))]
    text = "\n\n".join(part for part in parts if part)

    system_event = None
    if raw.get("messageType") not in (None, "message"):
        detail = mapping(raw.get("eventDetail")).get("@odata.type")
        system_event = _event_name(detail) or str(raw.get("messageType"))
        if not text:
            text = f"[System message: {system_event}]"

    deleted_at = parse_time(raw.get("deletedDateTime"))
    subject = raw.get("subject")
    reply_to = raw.get("replyToId")
    return Message(
        id=str(raw["id"]),
        conversation_id=conversation_id,
        author=author.strip() if isinstance(author, str) and author.strip() else None,
        author_id=author_id,
        sent_at=created(raw),
        text=text,
        system_event=system_event,
        subject=subject.strip() if isinstance(subject, str) and subject.strip() else None,
        reply_to_id=str(reply_to) if reply_to else None,
        edited_at=parse_time(raw.get("lastEditedDateTime")),
        is_deleted=deleted_at is not None,
        deleted_at=deleted_at,
        attachments=attachments,
    )


def _event_name(odata_type: Any) -> str | None:
    """'#microsoft.graph.membersAddedEventMessageDetail' → 'membersAdded'."""
    if not isinstance(odata_type, str) or not odata_type:
        return None
    name = odata_type.rsplit(".", 1)[-1]
    return name.removesuffix("EventMessageDetail") or name
