"""CSA catalog rows and Chat Service conversation rows → domain chats.

Pure normalization (no network). Naming needs people's display names, which the adapter looks up
between ``plan_names`` and ``NamePlan.apply``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from teams_connector.domain.models import Chat, ChatKind
from teams_connector.remote.chat_service_mapping import (
    mapping,
    member_id,
    message_author_id,
    message_datetime,
    optional_bool,
    preview,
    user_id,
)

SELF_CHAT_ID = "48:notes"
GROUP_NAME_LIMIT = 87  # stop adding names once the label would exceed this many characters

CHAT_SERVICE_CHAT_TYPES = {"chat", "onetoonechat", "meeting"}
CHAT_SERVICE_NON_CHAT_TYPES = {
    "space", "topic", "teamsteam", "teamsstandardchannel", "teamssharedchannel", "streamofnotes",
    "streamofnotifications", "streamofmentions", "streamofcalllogs", "streamofannotations", "streamofgeneric",
}  # fmt: skip


@dataclass(frozen=True)
class Me:
    """The signed-in user, as the catalog needs them."""

    user_id: str  # Entra object id, lower case
    name: str | None


def kind_from_type(value: Any) -> ChatKind | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    if normalized in {"meeting", "meetingchat", "call"}:
        return "meeting"
    if normalized in {"group", "groupchat", "groupconversation"}:
        return "group"
    if normalized in {"onetoone", "onetoonechat", "oneonone"}:
        return "one_on_one"
    if normalized in {"channel", "standardchannel", "sharedchannel"}:
        return "channel"
    return None


def complete_member_count(row: dict[str, Any], me: Me) -> int | None:
    """Count a roster only when it is versioned, identified, and includes the signed-in user."""
    version = row.get("rosterVersion")
    if not (
        (isinstance(version, int) and not isinstance(version, bool))
        or (isinstance(version, str) and version.strip())
    ):
        return None
    members = row.get("members")
    if not isinstance(members, list) or not members:
        return None
    ids: set[str] = set()
    for member in members:
        if not isinstance(member, dict):
            return None
        identifier = member_id(member)
        if not identifier:
            return None
        ids.add(identifier)
    return len(ids) if me.user_id in ids else None


def chat_kind(row: dict[str, Any], me: Me) -> ChatKind | None:
    """Explicit CSA semantics first (a meeting stays a meeting), then flags and roster size."""
    thread = mapping(row.get("threadProperties"))
    kinds = {
        kind
        for value in (row.get("chatType"), row.get("threadType"), thread.get("threadType"))
        if (kind := kind_from_type(value))
    }
    if "meeting" in kinds:
        return "meeting"
    if "channel" in kinds:
        return "channel"
    one_on_one = optional_bool(row.get("isOneOnOne"))
    count = complete_member_count(row, me)
    if count is not None and count > 2:
        return "group"
    if "one_on_one" in kinds or one_on_one is True:
        return "one_on_one"
    if "group" in kinds or one_on_one is False:
        return "group"
    return None


def row_id(row: dict[str, Any], *, channel: bool) -> str:
    keys = ("conversationId", "threadId", "id", "chatId") if channel else ("id",)
    value = next((v for key in keys if isinstance((v := row.get(key)), str) and v), None)
    if value is None:
        raise ValueError(
            f"Unexpected Teams catalog response: a {'channel' if channel else 'chat'} row has no id."
        )
    return value


def summarize_row(row: Any, *, channel: bool, me: Me) -> Chat | None:
    """One CSA chat or channel row. Deleted rows return None. An untitled chat is named by its id."""
    if not isinstance(row, dict):
        raise ValueError("Unexpected Teams catalog response: a row is not an object.")
    if optional_bool(row.get("isDeleted")) is True:
        return None
    chat_id = row_id(row, channel=channel)
    channel_info = mapping(row.get("channel"))
    team = mapping(row.get("team"))
    thread = mapping(row.get("threadProperties"))
    last = mapping(row.get("lastMessage"))
    title = (
        row.get("title") or row.get("displayName") or row.get("topic") or row.get("name")
        or thread.get("topic") or channel_info.get("title") or channel_info.get("displayName")
        or channel_info.get("name") or chat_id
    )  # fmt: skip
    team_title = row.get("teamName") or team.get("title") or team.get("displayName") or team.get("name")
    if channel and isinstance(team_title, str) and team_title.strip():
        title = channel_name(team_title, str(title))
    is_read = optional_bool(row.get("isRead"))
    unread = None if is_read is None else not is_read
    if unread is None:
        unread = optional_bool(row.get("isUnread"))
    kind: ChatKind | None = "channel" if channel else chat_kind(row, me)
    if chat_id == SELF_CHAT_ID:
        kind = "self"
    return Chat(
        id=chat_id,
        name=str(title),
        kind=kind,
        last_message_at=message_datetime(last),
        last_message_preview=preview(last.get("content")),
        unread=unread,
        is_empty=optional_bool(row.get("isEmptyConversation")),
    )


def channel_name(team: str, channel: str) -> str:
    """'Team · Channel' (just the channel name when it repeats the team name)."""
    team, channel = team.strip(), channel.strip()
    return channel if channel.casefold() == team.casefold() else f"{team} · {channel}"


def unnamed_group_name(names: list[str], participant_count: int) -> str | None:
    """A deterministic label for an untitled group: sorted names joined by ' | ', cut at about 87
    characters, with ' +N' for the people left out."""
    ordered = sorted(
        [name.strip() for name in names if isinstance(name, str) and name.strip()],
        key=lambda name: (name.casefold(), name),
    )
    if not ordered:
        return None
    shown: list[str] = []
    for name in ordered:
        if len(" | ".join([*shown, name])) > GROUP_NAME_LIMIT:
            if not shown:
                shown.append(name[:80].rstrip() + "…")
            break
        shown.append(name)
    omitted = max(0, participant_count - len(shown))
    label = " | ".join(shown)
    return f"{label} +{omitted}" if omitted else label


@dataclass
class NamePlan:
    """Untitled chats to name, and the people whose names the catalog does not carry."""

    missing_ids: list[str]
    _one_on_one: dict[str, list[Chat]] = field(default_factory=dict)
    _groups: list[tuple[Chat, list[str], dict[str, str], int]] = field(default_factory=list)
    _known: dict[str, str] = field(default_factory=dict)

    def apply(self, names: dict[str, str]) -> None:
        for person, chats in self._one_on_one.items():
            name = names.get(person)
            if name:
                for chat in chats:
                    chat.name = name
        for chat, ids, member_names, count in self._groups:
            resolved = [member_names.get(i) or self._known.get(i) or names.get(i) for i in ids]
            label = unnamed_group_name([n for n in resolved if n], count)
            if label:
                chat.name = label


def plan_names(rows: list[Any], chats: list[Chat], raw_users: Any, me: Me) -> NamePlan:
    """Name untitled one-on-one chats (person or app) and fully rostered unnamed groups; set the
    photo of one-on-one chats. Names the catalog carries are applied immediately."""
    by_id = {chat.id: chat for chat in chats}
    known: dict[str, str] = {}
    for user in raw_users if isinstance(raw_users, list) else []:
        if not isinstance(user, dict):
            continue
        name = user.get("displayName") or user.get("userPrincipalName")
        if not isinstance(name, str) or not name.strip():
            continue
        for value in (user.get("objectId"), user.get("mri")):
            identifier = user_id(value)
            if identifier and not identifier.startswith("28:"):
                known[identifier] = name.strip()

    plan = NamePlan(missing_ids=[], _known=known)
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_key = row.get("id")
        chat = by_id.get(row_key) if isinstance(row_key, str) else None
        if chat is None:
            continue
        raw_members = row.get("members")
        members: list[Any] = raw_members if isinstance(raw_members, list) else []
        ids: list[str] = []
        member_names: dict[str, str] = {}
        for member in members:
            if not isinstance(member, dict):
                continue
            identifier = member_id(member)
            if not identifier:
                continue
            if identifier not in ids:
                ids.append(identifier)
            name = member.get("displayName") or member.get("name")
            if isinstance(name, str) and name.strip():
                member_names[identifier] = name.strip()
        peers = [i for i in ids if i != me.user_id]

        if chat.kind == "one_on_one" and len(peers) == 1 and not peers[0].startswith("28:"):
            chat.photo_user_id = peers[0]
        if chat.name != chat.id:
            continue  # titled: nothing to name

        if chat.kind == "one_on_one" and len(peers) == 1:
            peer = peers[0]
            if peer.startswith("28:"):
                _name_app_chat(row, chat, ids, peer, me)
                continue
            name = member_names.get(peer) or known.get(peer)
            if name:
                chat.name = name
            else:
                plan._one_on_one.setdefault(peer, []).append(chat)
            continue

        count = complete_member_count(row, me)
        if chat.kind == "group" and count is not None and count > 2 and me.user_id in ids:
            group_ids = [i for i in peers if not i.startswith("28:")]
            plan._groups.append((chat, group_ids, member_names, count))
            for person in group_ids:
                if person not in member_names and person not in known:
                    plan._one_on_one.setdefault(person, [])
    plan.missing_ids = list(plan._one_on_one)
    return plan


def _name_app_chat(row: dict[str, Any], chat: Chat, ids: list[str], peer: str, me: Me) -> None:
    """An untitled one-on-one with an app: use the app's name only when the roster and the last
    message's sender confirm it is that app."""
    last = mapping(row.get("lastMessage"))
    app = mapping(mapping(last.get("properties")).get("fromAppMetadata"))
    name, app_id = app.get("name"), app.get("id")
    if (
        complete_member_count(row, me) == 2
        and me.user_id in ids
        and message_author_id(last) == peer
        and isinstance(app_id, str)
        and app_id.strip()
        and isinstance(name, str)
        and name.strip()
    ):
        chat.name = name.strip()


def self_chat(me: Me, existing: Chat | None = None) -> Chat:
    """The notes-to-self chat. CSA does not always list it; Chat Service always serves it."""
    label = (me.name or "").strip()
    name = (label if label.casefold().endswith(" (you)") else f"{label} (You)") if label else "Notes to self"
    if existing is None:
        return Chat(id=SELF_CHAT_ID, name=name, kind="self", photo_user_id=me.user_id)
    return existing.model_copy(update={"name": name, "kind": "self", "photo_user_id": me.user_id})


# ---------------------------------------------------------------------- Chat Service conversation rows


def service_row_class(conversation: dict[str, Any]) -> str:
    """'chat', 'non_chat' or 'unclassified', before merging a Chat Service row into the catalog."""
    if conversation.get("id") == SELF_CHAT_ID:
        return "chat"
    thread = mapping(conversation.get("threadProperties"))
    types = {
        value.casefold().replace("_", "").replace("-", "").replace(" ", "")
        for value in (thread.get("productThreadType"), thread.get("threadType"))
        if isinstance(value, str) and value.strip()
    }
    if types & CHAT_SERVICE_NON_CHAT_TYPES:
        return "non_chat"
    if types & CHAT_SERVICE_CHAT_TYPES:
        return "chat"
    return "unclassified"


def service_kind(product_type: Any, thread_type: Any, conversation_id: str) -> ChatKind | None:
    if conversation_id == SELF_CHAT_ID:
        return "self"
    product = re.sub(r"[^a-z0-9]", "", product_type.casefold()) if isinstance(product_type, str) else ""
    if product == "chat":
        return "group"
    for value in (product_type, thread_type):
        kind = kind_from_type(value)
        if kind:
            return kind
    return None


def summarize_service_row(conversation: dict[str, Any]) -> Chat:
    conversation_id = str(conversation["id"])
    props = mapping(conversation.get("properties"))
    thread = mapping(conversation.get("threadProperties"))
    last = mapping(conversation.get("lastMessage"))
    title = next(
        (
            value.strip()
            for value in (thread.get("topic"), props.get("displayName"), conversation.get("title"),
                          conversation.get("displayName"))
            if isinstance(value, str) and value.strip()
        ),
        conversation_id,
    )  # fmt: skip
    empty = optional_bool(conversation.get("isEmptyConversation"))
    if empty is None:
        empty = optional_bool(props.get("isEmptyConversation"))
    return Chat(
        id=conversation_id,
        name=title,
        kind=service_kind(thread.get("productThreadType"), thread.get("threadType"), conversation_id),
        last_message_at=message_datetime(last)
        or message_datetime({"originalArrivalTime": props.get("lastimreceivedtime")}),
        last_message_preview=preview(last.get("content")),
        is_empty=empty,
    )


def is_untitled_one_on_one_service_row(conversation: dict[str, Any], chat: Chat) -> bool:
    product = mapping(conversation.get("threadProperties")).get("productThreadType")
    return chat.name == chat.id and isinstance(product, str) and product.casefold() == "onetoonechat"
