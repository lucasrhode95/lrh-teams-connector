"""An in-memory Microsoft Teams tenant served through httpx.MockTransport.

It understands exactly the request shapes the connector sends: Teams AuthZ, the CSA catalog (snapshot
and delta), Chat Service history, members and listing, and the Graph endpoints for teams, channels, channel
threads, members, people, photos, sends and shared files. All data is synthetic.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx
from m365_auth import AccessToken, CacheStatus, ProfileStatus

from tests.fakes.msal_fakes import TENANT, USER_OID, jwt

CHAT_SERVICE = "https://emea.ng.msg.teams.microsoft.com"
GRAPH = "https://graph.microsoft.com/v1.0"
ME = USER_OID
ME_NAME = "Mia Example"
PEER = "33333333-3333-3333-3333-333333333333"
PEER_NAME = "Pat Peer"
THIRD = "44444444-4444-4444-4444-444444444444"
FOURTH = "55555555-5555-5555-5555-555555555555"


def ms(when: str) -> int:
    return int(datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp() * 1000)


def cs_message(
    message_id: str,
    when: str,
    text: str = "Hello",
    *,
    author: str = PEER,
    name: str | None = PEER_NAME,
    kind: str = "RichText/Html",
    edited: str | None = None,
    deleted: str | None = None,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A Chat Service message record."""
    properties: dict[str, Any] = {}
    if edited:
        properties["edittime"] = str(ms(edited))
    if deleted:
        properties["deletetime"] = str(ms(deleted))
    if files:
        properties["files"] = json.dumps(files)
    return {
        "id": message_id,
        "sequenceId": ms(when) // 1000,
        "originalarrivaltime": when,
        "messagetype": kind,
        "contenttype": "text",
        "content": "" if deleted else (f"<p>{text}</p>" if "Html" in kind else text),
        "imdisplayname": name,
        "from": f"{CHAT_SERVICE}/v1/users/ME/contacts/8:orgid:{author}",
        "properties": properties,
    }


def _channel_message(channel: FakeChannel, message_id: str) -> dict[str, Any] | None:
    """A channel post or reply by id, as Graph returns one channel message."""
    for root in channel.threads:
        if root["id"] == message_id:
            return root
        for reply in root.get("replies", []):
            if reply["id"] == message_id:
                return reply
    return None


def graph_message(
    message_id: str,
    when: str,
    text: str = "Post",
    *,
    author: str = PEER,
    name: str = PEER_NAME,
    reply_to: str | None = None,
    deleted: str | None = None,
    subject: str | None = None,
    replies: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    event: str | None = None,
) -> dict[str, Any]:
    """A Graph chatMessage (channel post or reply)."""
    return {
        "id": message_id,
        "replyToId": reply_to,
        "messageType": "systemEventMessage" if event else "message",
        "createdDateTime": when,
        "lastModifiedDateTime": deleted or when,
        "lastEditedDateTime": None,
        "deletedDateTime": deleted,
        "subject": subject,
        "from": None if deleted or event else {"user": {"id": author, "displayName": name}},
        "body": {"contentType": "html", "content": "" if deleted else f"<p>{text}</p>"},
        "attachments": attachments or [],
        "eventDetail": {"@odata.type": f"#microsoft.graph.{event}EventMessageDetail"} if event else None,
        "replies": replies or [],
    }


@dataclass
class FakeChat:
    id: str
    title: str | None = None
    chat_type: str | None = None  # CSA chatType: oneOnOne, group, meeting
    members: list[tuple[str, str | None]] = field(default_factory=list)  # (object id, member name)
    messages: list[dict[str, Any]] = field(default_factory=list)  # Chat Service records
    empty: bool | None = None
    in_csa: bool = True  # listed by the CSA snapshot (False: only the Chat Service listing has it)

    def csa_row(self) -> dict[str, Any]:
        last = max(self.messages, key=lambda m: m["originalarrivaltime"]) if self.messages else {}
        row: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "chatType": self.chat_type,
            "isOneOnOne": self.chat_type == "oneOnOne",
            "rosterVersion": 1,
            "members": [{"mri": f"8:orgid:{oid}", "objectId": oid} for oid, _ in self.members],
            "lastMessage": {
                "content": last.get("content"),
                "originalArrivalTime": last.get("originalarrivaltime"),
            },
            "isEmptyConversation": self.empty,
            "isRead": True,
        }
        return row


@dataclass
class FakeChannel:
    id: str
    team_id: str
    name: str
    threads: list[dict[str, Any]] = field(default_factory=list)  # Graph roots with "replies"
    members: list[tuple[str, str]] = field(default_factory=list)
    listed: bool = True  # returned by allChannels (False: found only by id)
    in_csa: bool = False
    last_activity: str | None = None  # what the private catalog reports


@dataclass
class FakeTeams:
    chats: dict[str, FakeChat] = field(default_factory=dict)
    channels: dict[str, FakeChannel] = field(default_factory=dict)
    teams: dict[str, str] = field(default_factory=dict)  # team id -> name
    users: dict[str, str] = field(default_factory=dict)  # object id -> display name (Graph directory)
    photos: dict[str, bytes] = field(default_factory=dict)
    files: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # sharing URL -> (name, bytes)
    images: dict[str, bytes] = field(default_factory=dict)  # AMS object id -> full-size image bytes
    hosted: dict[str, bytes] = field(default_factory=dict)  # Graph hosted-content id -> image bytes
    favorite_ids: set[str] = field(default_factory=set)  # chats in the Favorites sidebar folder
    presence_fails: bool = False  # the regional presence service answers 503
    delta_rows: list[dict[str, Any]] = field(default_factory=list)  # rows returned for the next delta
    deleted_chat_ids: list[str] = field(default_factory=list)
    csa_partial: bool = False
    expired_tokens: set[str] = field(default_factory=set)
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    fail_sends: str | None = None  # "network" or "504"
    token_counter: int = 0

    # ------------------------------------------------------------------ routing

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _folders(self) -> httpx.Response:
        """The sidebar folders: Favorites lists ``favorite_ids``; the other folder is empty."""
        favorites = [{"conversationId": chat_id} for chat_id in sorted(self.favorite_ids)]
        return httpx.Response(
            200,
            json={
                "conversationFolders": [
                    {"folderType": "Favorites", "conversationFolderItems": favorites},
                    {"folderType": "RecentChats", "conversationFolderItems": []},
                ]
            },
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path = request.url.path
        self.calls.append(f"{request.method} {url.hostname}{path}")
        if url.hostname == "authsvc.teams.microsoft.com":
            return httpx.Response(
                200,
                json={
                    "tokens": {"skypeToken": "skype-token", "expiresIn": 86400},
                    "regionGtms": {"chatService": CHAT_SERVICE},
                },  # fmt: skip
            )
        if url.hostname == "teams.microsoft.com" and path.endswith("/users/me/conversationFolders"):
            return self._folders()
        if url.hostname == "teams.microsoft.com" and path.endswith("/users/me/updates"):
            return self._csa(request)
        if url.hostname == "emea.ng.msg.teams.microsoft.com":
            return self._chat_service(request, path, parse_qs(url.query))
        if url.hostname == "eu-api.asm.skype.com":
            return self._ams(request, path)
        if url.hostname and url.hostname.endswith(".presence.teams.microsoft.com"):
            return self._presence(request)
        if url.hostname == "graph.microsoft.com":
            return self._graph(request, path.removeprefix("/v1.0"), parse_qs(url.query))
        if url.hostname and url.hostname.endswith(".sharepoint.com"):
            key = path.rsplit("/", 1)[-1]
            for name, data in self.files.values():
                if _share_key(name) == key:
                    return httpx.Response(
                        200, content=data, headers={"content-type": "application/octet-stream"}
                    )
        return httpx.Response(404, json={"error": {"code": "NotFound", "message": "no route"}})

    # ------------------------------------------------------------------ CSA

    def _csa(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("x-ms-synctoken")
        if token in self.expired_tokens:
            return httpx.Response(410, json={"errorCode": "SyncTokenExpired"})
        self.token_counter += 1
        metadata = {"syncToken": f"sync-{self.token_counter}"}
        if token is None:
            metadata.update(isPartialData=self.csa_partial, hasMoreChats=False)
            chats = [c.csa_row() for c in self.chats.values() if c.in_csa]
            channels = [
                {"id": ch.id, "title": ch.name, "teamName": self.teams[ch.team_id],
                 "lastMessage": {"originalArrivalTime": ch.last_activity, "content": "<p>latest</p>"}}
                for ch in self.channels.values()
                if ch.in_csa
            ]  # fmt: skip
        else:
            chats = self.delta_rows + [{"id": i, "isDeleted": True} for i in self.deleted_chat_ids]
            channels = []
            self.delta_rows, self.deleted_chat_ids = [], []
        users = [{"objectId": ME, "displayName": ME_NAME}]
        return httpx.Response(
            200, json={"metadata": metadata, "chats": chats, "channels": channels, "users": users}
        )

    # ------------------------------------------------------------------ Chat Service

    def _chat_service(self, request: httpx.Request, path: str, query: dict[str, list[str]]) -> httpx.Response:
        if request.headers.get("authentication") != "skypetoken=skype-token":
            return httpx.Response(401, json={"errorCode": "Unauthorized"})
        parts = path.strip("/").split("/")
        if parts[:4] == ["v1", "users", "ME", "conversations"] and len(parts) == 4:
            rows = [
                {"id": c.id, "threadProperties": {"topic": c.title or "", "productThreadType": "OneToOneChat"
                                                  if c.chat_type == "oneOnOne" else "Chat"}}
                for c in self.chats.values()
            ]  # fmt: skip
            return httpx.Response(200, json={"conversations": rows, "_metadata": {}})
        if parts[:4] == ["v1", "users", "ME", "conversations"] and parts[-1] == "messages":
            chat = self.chats.get(parts[4])
            if chat is None:
                return httpx.Response(404, json={"errorCode": "NotFound"})
            start = int(query.get("startTime", ["0"])[0])
            end = int(query.get("endTime", [str(2**62)])[0])
            size = int(query.get("pageSize", ["100"])[0])
            offset = int(query.get("syncState", ["0"])[0])
            ordered = sorted(
                (m for m in chat.messages if start <= ms(m["originalarrivaltime"]) <= end),
                key=lambda m: m["originalarrivaltime"],
                reverse=True,
            )
            page = ordered[offset : offset + size]
            link = None
            if offset + size < len(ordered):
                params = {"startTime": str(start), "pageSize": str(size), "view": "msnp24Equivalent",
                          "syncState": str(offset + size)}  # fmt: skip
                link = f"{CHAT_SERVICE}{path}?{urlencode(params)}"
            return httpx.Response(200, json={"messages": page, "_metadata": {"backwardLink": link}})
        if (
            parts[:4] == ["v1", "users", "ME", "conversations"]
            and len(parts) == 7
            and parts[5] == "messages"
            and parts[4] in self.chats
        ):
            # one message by id; Teams answers an id it does not hold with 403
            rows = [m for m in self.chats[parts[4]].messages if m["id"] == parts[6]]
            if not rows:
                return httpx.Response(403, json={"errorCode": "Forbidden"})
            return httpx.Response(200, json=rows[0])
        if parts[:2] == ["v1", "threads"] and parts[-1] == "members":
            chat = self.chats.get(parts[2])
            if chat is None or chat.id == "48:notes":
                return httpx.Response(403, json={"errorCode": "Forbidden"})
            members = [{"id": f"8:orgid:{oid}", "userDisplayName": name or ""} for oid, name in chat.members]
            return httpx.Response(200, json={"members": members})
        return httpx.Response(404, json={"errorCode": "NotFound"})

    # ------------------------------------------------------------------ Graph

    def _presence(self, request: httpx.Request) -> httpx.Response:
        """Regional Unified Presence: the teams.spaces bearer is accepted; one row per requested MRI. The
        response carries fields the connector does not report, so they can be checked as dropped."""
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"errorCode": "911"})
        if self.presence_fails:
            return httpx.Response(503, json={"errorCode": "Unavailable"})
        rows = json.loads(request.content)
        presence = {
            "availability": "Available",
            "activity": "InACall",
            "deviceType": "Desktop",
            "lastActiveTime": "2026-09-05T10:00:00.000Z",
            "capabilities": ["synthetic"],
            "calendarData": {"status": "synthetic"},
            "workLocation": {"type": "synthetic"},
            "forcedAvailability": {"availability": "Away"},
            "sourceNetwork": "synthetic",
        }
        return httpx.Response(
            200,
            json=[
                {"mri": row["mri"], "etag": "1", "etagMatch": False, "status": 200, "presence": presence}
                for row in rows
            ],  # fmt: skip
        )

    def _ams(self, request: httpx.Request, path: str) -> httpx.Response:
        """AMS full-size views of pasted images. The connector sends the Skype token as ``skype_token``."""
        if not request.headers.get("authorization", "").startswith("skype_token "):
            return httpx.Response(401, json={"error": "unauthorized"})
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[:2] == ["v1", "objects"] and parts[3:] == ["views", "imgpsh_fullsize"]:
            data = self.images.get(parts[2])
            if data is not None:
                return httpx.Response(200, content=data, headers={"content-type": "image/png"})
        return _not_found()

    def _graph(self, request: httpx.Request, path: str, query: dict[str, list[str]]) -> httpx.Response:
        parts = path.strip("/").split("/")
        if len(parts) == 9 and parts[6] == "hostedContents" and parts[8] == "$value":
            data = self.hosted.get(parts[7])
            if data is None:
                return _not_found()
            return httpx.Response(200, content=data, headers={"content-type": "image/png"})
        if request.method == "POST" and path == "/$batch":
            body = json.loads(request.content)
            responses = []
            for item in body["requests"]:
                path_only = unquote(item["url"].split("?")[0])  # Graph decodes batch paths too
                sub = self._graph(httpx.Request("GET", GRAPH + item["url"]), path_only, {})
                responses.append({"id": item["id"], "status": sub.status_code, "body": sub.json()})
            return httpx.Response(200, json={"responses": responses})
        if request.method == "POST" and parts[-1] == "messages":
            if self.fail_sends == "network":
                raise httpx.ReadError("connection reset", request=request)
            if self.fail_sends == "504":
                return httpx.Response(504, json={"error": {"code": "GatewayTimeout"}})
            body = json.loads(request.content)
            self.sent.append((path, body))
            return httpx.Response(
                201,
                json={"id": f"{1790000000000 + len(self.sent)}", "createdDateTime": "2026-10-02T12:00:00Z"},
            )
        if path == "/me/joinedTeams":
            return _page([{"id": t, "displayName": n} for t, n in self.teams.items()])
        if parts[0] == "teams" and len(parts) == 3 and parts[2] == "allChannels":
            rows = [
                {"id": c.id, "displayName": c.name, "membershipType": "standard"}
                for c in self.channels.values()
                if c.team_id == parts[1] and c.listed
            ]
            return _page(rows)
        if parts[0] == "teams" and len(parts) >= 4 and parts[2] == "channels":
            channel = self.channels.get(parts[3])
            if channel is None or channel.team_id != parts[1]:
                return _not_found()
            if len(parts) == 4:
                return httpx.Response(200, json={"id": channel.id, "displayName": channel.name,
                                                 "membershipType": "shared"})  # fmt: skip
            if parts[4] == "members":
                return _page([{"userId": oid, "displayName": name} for oid, name in channel.members])
            if parts[4] == "messages" and len(parts) == 5:
                return self._threads(channel, query)
            if parts[4] == "messages" and len(parts) == 6:
                found = _channel_message(channel, parts[5])
                return httpx.Response(200, json=found) if found is not None else _not_found()
        if parts[0] == "users" and len(parts) == 2:
            name = self.users.get(parts[1])
            return httpx.Response(200, json={"id": parts[1], "displayName": name}) if name else _not_found()
        if parts[0] == "users" and parts[2:] == ["photos", "64x64", "$value"]:
            data = self.photos.get(parts[1])
            if data is None:
                return httpx.Response(404, json={"error": {"code": "ImageNotFound", "message": "no photo"}})
            return httpx.Response(200, content=data, headers={"content-type": "image/jpeg"})
        if parts[0] == "shares":
            share = parts[1]
            url = base64.urlsafe_b64decode(share[2:] + "=" * (-len(share[2:]) % 4)).decode()
            entry = self.files.get(url)
            if entry is None:
                return _not_found()
            name, data = entry
            if parts[-1] == "content":
                return httpx.Response(
                    302, headers={"location": f"https://tenant.sharepoint.com/dl/{_share_key(name)}"}
                )
            key = _share_key(name)
            return httpx.Response(
                200,
                json={
                    "id": f"drive-item-{key}",
                    "name": name,
                    "size": len(data),
                    "eTag": f'"{{{key}}},1"',
                    "file": {"mimeType": "text/plain"},
                },
            )
        return _not_found()

    def _threads(self, channel: FakeChannel, query: dict[str, list[str]]) -> httpx.Response:
        def activity(root: dict[str, Any]) -> str:
            return max([root["createdDateTime"], *[r["createdDateTime"] for r in root.get("replies", [])]])

        ordered = sorted(channel.threads, key=activity, reverse=True)
        top = int(query.get("$top", ["20"])[0])
        skip = int(query.get("$skiptoken", ["0"])[0])
        page = ordered[skip : skip + top]
        data: dict[str, Any] = {"value": page}
        if skip + top < len(ordered):
            data["@odata.nextLink"] = (
                f"{GRAPH}/teams/{channel.team_id}/channels/{channel.id}/messages?"
                f"{urlencode({'$top': top, '$expand': 'replies', '$skiptoken': skip + top})}"
            )
        return httpx.Response(200, json=data)


def _page(rows: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"value": rows})


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": "NotFound", "message": "Not found"}})


def _share_key(name: str) -> str:
    return base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")


class FakeTokens:
    """Token provider stand-in: every profile yields a token for the synthetic account."""

    def __init__(self) -> None:
        self.object_id = ME

    def get_token(self, profile: str, **_: object) -> AccessToken:
        claims = {
            "tid": TENANT,
            "oid": self.object_id,
            "upn": "mia@example.com",
            "name": ME_NAME,
            "aud": profile,
        }
        return AccessToken(profile=profile, value=jwt(claims), source="cache", expires_on=None)

    def sign_in_command(self, profile: str = "teams.spaces") -> str:
        return "teams-connector auth"

    def status(self) -> CacheStatus:
        rows = tuple(
            ProfileStatus(profile, "c", (), profile, True, None)
            for profile in (
                "outlook.graph",
                "outlook.api",
                "m365.search",
                "teams.spaces",
                "teams.csa",
                "teams.graph",
            )
        )
        return CacheStatus("encrypted", Path("x"), True, (), rows)


def sample_tenant() -> FakeTeams:
    """A small tenant: a one-on-one, an unnamed group, a meeting, the self chat, and a team channel."""
    fake = FakeTeams(
        users={ME: ME_NAME, PEER: PEER_NAME, THIRD: "Quinn Third", FOURTH: "Ray Fourth"},
        photos={PEER: b"jpeg-of-pat"},
        teams={"team-1": "Platform"},
    )
    fake.chats["19:one@unq.gbl.spaces"] = FakeChat(
        id="19:one@unq.gbl.spaces",
        chat_type="oneOnOne",
        members=[(ME, ME_NAME), (PEER, None)],
        messages=[
            cs_message("1001", "2026-09-01T10:00:00.000Z", "First hello"),
            cs_message(
                "1002", "2026-09-02T10:00:00.000Z", "Second, about the budget review", author=ME, name=ME_NAME
            ),
            cs_message(
                "1003",
                "2026-09-03T10:00:00.000Z",
                "Third",
                files=[
                    {
                        "fileName": "notes.txt",
                        "fileInfo": {"shareUrl": "https://tenant.sharepoint.com/s/notes"},
                    }
                ],
            ),
        ],  # fmt: skip
    )
    fake.chats["19:group@thread.v2"] = FakeChat(
        id="19:group@thread.v2",
        chat_type="group",
        members=[(ME, None), (PEER, None), (THIRD, None)],
        messages=[
            cs_message("2001", "2026-08-15T09:00:00.000Z", "Group news", author=THIRD, name="Quinn Third")
        ],
    )
    fake.chats["19:meeting_abc@thread.v2"] = FakeChat(
        id="19:meeting_abc@thread.v2",
        title="Sprint review",
        chat_type="meeting",
        members=[(ME, None), (PEER, None)],
        messages=[cs_message("3001", "2026-07-01T09:00:00.000Z", "Agenda")],
    )
    fake.chats["48:notes"] = FakeChat(
        id="48:notes",
        messages=[
            cs_message("4001", "2026-09-05T08:00:00.000Z", "Reminder to self", author=ME, name=ME_NAME)
        ],
        in_csa=False,
    )
    fake.chats["19:empty@thread.v2"] = FakeChat(
        id="19:empty@thread.v2", title="Quiet", chat_type="group", empty=True
    )
    fake.channels["19:general@thread.tacv2"] = FakeChannel(
        id="19:general@thread.tacv2",
        team_id="team-1",
        name="General",
        members=[(ME, ME_NAME), (FOURTH, "Ray Fourth")],
        in_csa=True,
        last_activity="2026-09-04T12:00:00.000Z",
        threads=[
            graph_message("5001", "2026-09-01T12:00:00Z", "Launch plan", subject="Launch", replies=[
                graph_message("5002", "2026-09-04T12:00:00Z", "Agreed", reply_to="5001", author=FOURTH,
                              name="Ray Fourth"),
            ]),
            graph_message("5003", "2026-08-01T12:00:00Z", "Old post"),
        ],
    )  # fmt: skip
    fake.files["https://tenant.sharepoint.com/s/notes"] = ("notes.txt", b"shared notes")
    return fake
