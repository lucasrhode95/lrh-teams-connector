from __future__ import annotations

import html
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from teams_exporter.token_provider import AuthenticationRequiredError, TokenProvider


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChatSummary:
    """Small, normalized view of a chat returned by Teams."""

    id: str
    display_name: str
    last_message_at: datetime | None = None
    last_message_preview: str | None = None
    unread: bool | None = None
    chat_type: str | None = None
    avatar_url: str | None = None
    is_self_chat: bool = False


@dataclass(slots=True)
class ChatCatalogCoverage:
    """Provenance and completeness for one normalized chat catalog read."""

    catalog_complete: bool
    catalog_source: str
    catalog_scope: str
    csa_reported_partial: bool | None
    chat_service_unique_rows: int | None = None
    fallback_chats_added: int = 0
    non_chat_service_rows_excluded: int = 0
    unclassified_service_rows_excluded: int = 0
    fallback_name_lookups_attempted: int = 0
    fallback_chats_without_resolved_name: int = 0


class ExtractionCancelled(RuntimeError):
    """Raised when a caller asks an in-progress history read to stop."""


class _TransientTeamsRequestError(RuntimeError):
    def __init__(self, message: str, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _TeamsHttpError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _MiddleTierRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow picture redirects only when they stay on the token's host."""

    def __init__(self, middle_tier_url: str) -> None:
        super().__init__()
        self._middle_tier_netloc = urlsplit(middle_tier_url).netloc.lower()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(urljoin(req.full_url, newurl))
        if (
            target.scheme != "https"
            or target.netloc.lower() != self._middle_tier_netloc
            or target.username
            or target.password
        ):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class TeamsConnector:
    """Read chat catalogs and message history through Teams service APIs."""

    PROFILE_PICTURE_PATH = re.compile(
        r"(?:^|/)users/[A-Za-z0-9:@._-]+/profilepicturev2"
        r"(?:/[A-Za-z0-9:@._-]+)?$",
        re.IGNORECASE,
    )

    TEAMS_CLIENT_ID = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"
    SPACES_SCOPE = "https://api.spaces.skype.com/.default"
    CSA_SCOPE = "https://chatsvcagg.teams.microsoft.com/.default"
    AUTHZ_URL = "https://authsvc.teams.microsoft.com/v1.0/authz"
    CSA_STARTUP_URL = (
        "https://teams.microsoft.com/api/csa/api/v1/teams/users/me"
        "?isPrefetch=false&enableMembershipSummary=true"
    )
    CSA_UPDATES_URL = "https://teams.microsoft.com/api/csa/api/v1/teams/users/me/updates"
    SELF_CHAT_ID = "48:notes"
    CHAT_CATALOG_SCOPE = (
        "CSA chat, meeting, and channel entries plus the known notes-to-self chat; "
        "Teams service streams are excluded."
    )
    CHAT_SERVICE_CHAT_TYPES = {"chat", "onetoonechat", "meeting"}
    CHAT_SERVICE_NON_CHAT_TYPES = {
        "space",
        "topic",
        "teamsteam",
        "teamsstandardchannel",
        "teamssharedchannel",
        "streamofnotes",
        "streamofnotifications",
        "streamofmentions",
        "streamofcalllogs",
        "streamofannotations",
        "streamofgeneric",
    }
    MAX_FALLBACK_NAME_LOOKUPS = 20
    READ_REQUEST_MAX_ATTEMPTS = 4
    READ_RETRY_BASE_SECONDS = 0.25
    READ_RETRY_MAX_SECONDS = 8.0
    READ_RETRY_AFTER_MAX_SECONDS = 10.0
    RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
    _MESSAGE_AUTHOR_MRI = re.compile(
        r"(?i)(?<![a-z0-9])(8:orgid:[0-9a-f]{8}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])"
    )
    _OBJECT_ID = re.compile(
        r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}"
    )
    MESSAGE_PAGE_SIZE = 100
    REQUEST_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        tenant: str = "organizations",
        cache_path: str | Path | None = None,
        unsecure: bool = False,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self.token_provider = token_provider or TokenProvider(
            tenant=tenant,
            client_id=self.TEAMS_CLIENT_ID,
            cache_path=cache_path,
            unsecure=unsecure,
        )
        self._spaces_access_token: str | None = None
        self._csa_access_token: str | None = None
        self._skype_token: str | None = None
        self._spaces_claims: dict[str, Any] = {}
        self._authz_response: dict[str, Any] = {}
        self._chat_service_url: str | None = None
        self._middle_tier_url: str | None = None
        self._auth_sources: dict[str, str] = {}
        self._last_client_message_id = 0
        self.last_request_retry_count = 0
        self.last_message_read_coverage: dict[str, Any] = {
            "complete": False,
            "pages_fetched": 0,
            "raw_items_scanned": 0,
            "messages_retrieved": 0,
            "duplicate_messages_skipped": 0,
            "missing_timestamp_messages_excluded": 0,
            "retry_count": 0,
        }
        self.catalog_coverage = ChatCatalogCoverage(
            catalog_complete=False,
            catalog_source="not loaded",
            catalog_scope=self.CHAT_CATALOG_SCOPE,
            csa_reported_partial=None,
        )

    def login(self, *, interactive: bool = True) -> None:
        """Restore the cached session, optionally starting device-code login.

        MCP callers must use ``interactive=False`` so an expired or missing
        session returns a clear error instead of waiting on a terminal prompt.
        """
        logger.debug("Teams authentication check started interactive=%s", interactive)
        spaces_token, csa_token = self.token_provider.get_access_tokens(
            (self.SPACES_SCOPE, self.CSA_SCOPE),
            interactive=interactive,
        )

        self._auth_sources = {
            "spaces": spaces_token.source,
            "csa": csa_token.source,
        }
        self._spaces_access_token = spaces_token.value
        self._csa_access_token = csa_token.value
        self._spaces_claims = self._decode_jwt_payload(self._spaces_access_token)
        self._authz_response = self._request_teams_authz()

        tokens = self._authz_response.get("tokens") or {}
        nested_skype = self._authz_response.get("skypeToken") or {}
        self._skype_token = (
            tokens.get("skypeToken")
            or tokens.get("skypetoken")
            or nested_skype.get("skypetoken")
        )
        if not self._skype_token:
            raise RuntimeError("Teams AuthZ succeeded without returning a Skype token.")

        region_gtms = self._authz_response.get("regionGtms") or {}
        self._chat_service_url = region_gtms.get("chatService")
        self._middle_tier_url = region_gtms.get("middleTier")
        if not self._chat_service_url:
            raise RuntimeError("Teams AuthZ did not return regionGtms.chatService.")
        logger.info(
            "Teams authentication accepted token_sources=spaces:%s,csa:%s",
            self._auth_sources["spaces"],
            self._auth_sources["csa"],
        )

    def get_user_metadata(self) -> dict[str, Any]:
        """Return non-secret account fields from the Teams access token."""
        self._require_logged_in()
        claims = self._spaces_claims
        return {
            "name": claims.get("name"),
            "given_name": claims.get("given_name"),
            "family_name": claims.get("family_name"),
            "username": (
                claims.get("preferred_username")
                or claims.get("upn")
                or claims.get("unique_name")
            ),
            "user_id": claims.get("oid"),
            "tenant_id": claims.get("tid"),
            "country": claims.get("ctry"),
            "tenant_country": claims.get("tenant_ctry"),
        }

    def get_profile_picture_uri(
        self,
        user_id: str | None = None,
        display_name: str | None = None,
    ) -> str | None:
        """Build the Teams Web profile-picture URI without fetching the image."""
        current_user_id = self._profile_identifier(self._spaces_claims.get("oid"))
        target_id = user_id or current_user_id
        if not isinstance(target_id, str):
            return None
        target_id = target_id.strip()
        middle_tier = urlsplit(self._middle_tier_url or "")
        if not target_id or middle_tier.scheme != "https" or not middle_tier.netloc:
            return None

        if target_id.lower().startswith("8:orgid:"):
            target_mri = "8:orgid:" + target_id.split(":", 2)[2]
        elif ":" in target_id:
            return None
        else:
            target_mri = f"8:orgid:{target_id}"

        base_path = middle_tier.path.rstrip("/")
        if current_user_id:
            picture_path = (
                f"{base_path}/beta/users/{quote(current_user_id, safe='')}"
                f"/profilepicturev2/{quote(target_mri, safe=':')}"
            )
        else:
            picture_path = (
                f"{base_path}/beta/users/{quote(target_mri, safe=':')}"
                "/profilepicturev2"
            )
        name = display_name
        if name is None and target_id == current_user_id:
            name = self._spaces_claims.get("name")
        query = urlencode({"displayname": name or "", "size": "HR64x64"})
        return urlunsplit((middle_tier.scheme, middle_tier.netloc, picture_path, query, ""))

    def can_fetch_profile_picture(self, url: str) -> bool:
        """Whether this profile-picture URL belongs to the authenticated Middle Tier."""
        if not isinstance(url, str):
            return False
        expected = urlsplit(self._middle_tier_url or "")
        actual = urlsplit(url)
        return (
            actual.scheme == "https"
            and bool(expected.netloc)
            and actual.netloc.lower() == expected.netloc.lower()
            and bool(self.PROFILE_PICTURE_PATH.search(actual.path))
        )

    def get_profile_picture(self, url: str) -> tuple[bytes, str]:
        """Fetch one profile picture with the saved Teams Web bearer token."""
        self._require_logged_in()
        if not self.can_fetch_profile_picture(url):
            raise RuntimeError("Refusing a profile picture URL outside the configured Middle Tier.")
        self._validate_service_url(url, self._middle_tier_url, "Middle Tier")

        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._spaces_access_token}",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "User-Agent": "Microsoft Teams compatible client",
            },
        )
        opener = urllib.request.build_opener(
            _MiddleTierRedirectHandler(self._middle_tier_url or "")
        )
        max_image_bytes = 2_000_000
        for attempt in range(1, self.READ_REQUEST_MAX_ATTEMPTS + 1):
            retry_after = None
            try:
                with opener.open(request, timeout=self.REQUEST_TIMEOUT_SECONDS) as response:
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if not content_type.startswith("image/"):
                        raise RuntimeError("Teams returned a non-image profile picture response.")
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > max_image_bytes:
                        raise RuntimeError("Teams profile picture exceeded the image size limit.")
                    content = response.read(max_image_bytes + 1)
                    if len(content) > max_image_bytes:
                        raise RuntimeError("Teams profile picture exceeded the image size limit.")
                    return content, content_type
            except urllib.error.HTTPError as exc:
                location = self._safe_location(url)
                if exc.code in {401, 403}:
                    exc.close()
                    self._clear_session()
                    raise AuthenticationRequiredError(
                        "Teams no longer accepts the stored credentials for this request. "
                        "Run `python -m teams_exporter auth` and retry."
                    ) from None
                if exc.code not in self.RETRYABLE_HTTP_STATUSES:
                    status = exc.code
                    exc.close()
                    raise RuntimeError(
                        f"Teams profile picture request failed with HTTP {status}."
                    ) from None
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt >= self.READ_REQUEST_MAX_ATTEMPTS:
                    raise RuntimeError(
                        "Teams profile picture request failed after "
                        f"{self.READ_REQUEST_MAX_ATTEMPTS} attempts: "
                        f"{self._safe_exception_detail(exc)}"
                    ) from None

            if attempt >= self.READ_REQUEST_MAX_ATTEMPTS:
                raise RuntimeError(
                    "Teams profile picture request failed after "
                    f"{self.READ_REQUEST_MAX_ATTEMPTS} attempts."
                ) from None
            time.sleep(self._read_retry_delay(attempt, retry_after))

        raise RuntimeError("Teams profile picture request failed without a response.")

    def get_cached_account_metadata(self) -> dict[str, Any] | None:
        """Return the first cached MSAL account without making network requests.

        This lets local archive searches select the account-specific database
        without attempting sign-in or contacting Microsoft.
        """
        account = self.token_provider.get_cached_account()
        # Read the cached account directly. Creating PublicClientApplication
        # can perform OpenID metadata discovery, which would make local-only
        # archive searches depend on network access.
        if not account:
            return None
        return {
            "username": account.get("username"),
            "user_id": account.get("local_account_id"),
            "tenant_id": account.get("tenant_id"),
            "home_account_id": account.get("home_account_id"),
        }

    def get_connection_metadata(self) -> dict[str, Any]:
        """Return connection diagnostics without exposing credentials."""
        self._require_logged_in()
        expires_at = self._spaces_claims.get("exp")
        if isinstance(expires_at, (int, float)):
            expires_at = datetime.fromtimestamp(
                expires_at, tz=timezone.utc
            ).isoformat()
        return {
            "authentication_sources": dict(self._auth_sources),
            "client_id": self._spaces_claims.get("appid")
            or self._spaces_claims.get("azp"),
            "audience": self._spaces_claims.get("aud"),
            "scopes": self._spaces_claims.get("scp"),
            "spaces_access_token_expires_at": expires_at,
            "region": self._authz_response.get("region"),
            "region_gtms": self._authz_response.get("regionGtms"),
        }

    @property
    def has_active_session(self) -> bool:
        """Whether this connector currently holds a completed Teams session."""
        return bool(
            self._spaces_access_token
            and self._csa_access_token
            and self._skype_token
            and self._chat_service_url
            and self._middle_tier_url
        )

    def get_chat_updates(self, sync_token: str | None = None) -> dict[str, Any]:
        """Fetch a CSA bootstrap or delta and normalize chat and channel rows.

        A rejected/expired continuation is recovered by requesting a fresh
        snapshot from the same updates route without the sync-token header.
        The caller persists the returned token atomically with the rows.
        """
        self._require_logged_in()
        if sync_token is not None and (not isinstance(sync_token, str) or not sync_token):
            raise ValueError("sync_token must be a non-empty string or None.")

        used_sync_token = sync_token is not None
        try:
            payload = self._request_csa(
                self.CSA_UPDATES_URL,
                sync_token=sync_token,
            )
            if used_sync_token and self._sync_token_expired(payload):
                payload = self._request_csa(self.CSA_UPDATES_URL)
                used_sync_token = False
        except _TeamsHttpError as exc:
            if not used_sync_token or exc.status_code not in {400, 410}:
                raise
            payload = self._request_csa(self.CSA_UPDATES_URL)
            used_sync_token = False

        metadata = self._mapping(payload.get("metadata"))
        sync_token_value = metadata.get("syncToken") or metadata.get("forwardSyncToken")
        if not isinstance(sync_token_value, str) or not sync_token_value.strip():
            raise RuntimeError("Teams chat updates did not include a sync token.")

        raw_chats = payload.get("chats", [])
        raw_channels = payload.get("channels", [])
        if not isinstance(raw_chats, list) or not isinstance(raw_channels, list):
            raise RuntimeError("Unexpected Teams chat update response: chat or channel rows are invalid.")

        normalized: list[ChatSummary] = []
        deleted_ids: list[str] = []
        for row in raw_chats:
            if isinstance(row, dict) and self._optional_bool(row.get("isDeleted")) is True:
                deleted_ids.append(self._csa_update_conversation_id(row, "chat"))
                continue
            summary = self._summarize_csa_update_row(row, entity_type="chat")
            if summary is not None:
                normalized.append(summary)
        for row in raw_channels:
            if isinstance(row, dict) and self._optional_bool(row.get("isDeleted")) is True:
                deleted_ids.append(self._csa_update_conversation_id(row, "channel"))
                continue
            summary = self._summarize_csa_update_row(row, entity_type="channel")
            if summary is not None:
                normalized.append(summary)

        if not used_sync_token:
            # A bootstrap snapshot is expected to be complete. If CSA explicitly
            # says otherwise, preserve the established paginated catalog fallback.
            is_partial = metadata.get("isPartialData") is True or metadata.get("hasMoreChats") is True
            if is_partial:
                known_ids = {chat.id for chat in normalized}
                for chat in self.list_chats():
                    if chat.id not in known_ids:
                        normalized.append(chat)
                        known_ids.add(chat.id)
            self._ensure_self_chat(normalized)

        self._resolve_untitled_one_to_one_names(
            raw_chats,
            normalized,
            payload.get("users"),
        )
        self._add_one_to_one_profile_picture_uris(raw_chats, normalized)

        oldest = datetime.min.replace(tzinfo=timezone.utc)
        ordered = sorted(
            normalized,
            key=lambda chat: chat.last_message_at or oldest,
            reverse=True,
        )
        self.catalog_coverage = ChatCatalogCoverage(
            catalog_complete=(
                metadata.get("isPartialData") is False
                and metadata.get("hasMoreChats") is False
            ),
            catalog_source="CSA updates" + (" bootstrap" if not used_sync_token else " delta"),
            catalog_scope=self.CHAT_CATALOG_SCOPE,
            csa_reported_partial=(
                True
                if metadata.get("isPartialData") is True or metadata.get("hasMoreChats") is True
                else False
                if metadata.get("isPartialData") is False and metadata.get("hasMoreChats") is False
                else None
            ),
        )
        return {
            "chats": ordered,
            "sync_token": sync_token_value.strip(),
            "is_bootstrap": not used_sync_token,
            "deleted_ids": deleted_ids,
        }

    @staticmethod
    def _csa_update_conversation_id(row: dict[str, Any], entity_type: str) -> str:
        keys = (
            ("conversationId", "threadId", "id", "chatId")
            if entity_type == "channel"
            else ("id",)
        )
        conversation_id = next(
            (
                value
                for key in keys
                if isinstance((value := row.get(key)), str) and value
            ),
            None,
        )
        if not isinstance(conversation_id, str):
            raise RuntimeError("Unexpected Teams chat update response: a deleted row has no conversation ID.")
        return conversation_id

    def _summarize_csa_update_row(
        self,
        row: Any,
        entity_type: str,
    ) -> ChatSummary | None:
        if not isinstance(row, dict):
            raise RuntimeError("Unexpected Teams chat update response: a row is not an object.")
        if self._optional_bool(row.get("isDeleted")) is True:
            return None
        channel = self._mapping(row.get("channel"))
        team = self._mapping(row.get("team"))
        if entity_type == "channel":
            chat_id = next(
                (
                    value
                    for key in ("conversationId", "threadId", "id", "chatId")
                    if isinstance((value := row.get(key)), str) and value
                ),
                None,
            )
        else:
            chat_id = row.get("id")
        if not isinstance(chat_id, str) or not chat_id:
            raise RuntimeError("Unexpected Teams chat update response: a row has no conversation ID.")

        last_message = self._mapping(row.get("lastMessage"))
        title = (
            row.get("title")
            or row.get("displayName")
            or row.get("topic")
            or row.get("name")
            or channel.get("title")
            or channel.get("displayName")
            or channel.get("name")
            or chat_id
        )
        team_title = row.get("teamName") or team.get("title") or team.get("displayName") or team.get("name")
        if entity_type == "channel" and isinstance(team_title, str) and team_title.strip():
            if str(title).casefold() != team_title.strip().casefold():
                title = f"{team_title.strip()} · {title}"

        content = last_message.get("content")
        preview = content.strip()[:240] if isinstance(content, str) else None
        is_read = self._optional_bool(row.get("isRead"))
        unread = None if is_read is None else not is_read
        if unread is None:
            unread = self._optional_bool(row.get("isUnread"))
        chat_type = self._csa_chat_type(row, entity_type)

        avatar_url = self._avatar_url_from_rows(
            row,
            channel,
            team,
            self._mapping(row.get("threadProperties")),
            self._mapping(row.get("image")),
            self._mapping(row.get("avatar")),
        )
        return ChatSummary(
            id=chat_id,
            display_name=str(title),
            last_message_at=self._message_datetime(last_message),
            last_message_preview=preview,
            unread=unread,
            chat_type=chat_type,
            avatar_url=avatar_url,
            is_self_chat=chat_id == self.SELF_CHAT_ID,
        )

    def _csa_chat_type(self, row: dict[str, Any], entity_type: str = "chat") -> str | None:
        """Normalize CSA's generic chat type using one-to-one and roster data."""
        if entity_type == "channel":
            return "channel"

        one_to_one = self._optional_bool(row.get("isOneOnOne"))
        member_count = self._complete_csa_member_count(row)
        if one_to_one is False or (member_count is not None and member_count > 2):
            return "group"
        if one_to_one is True:
            return "oneOnOne"

        raw_chat_type = row.get("chatType")
        if not isinstance(raw_chat_type, str) or not raw_chat_type.strip():
            return None
        normalized = raw_chat_type.casefold().replace("_", "").replace("-", "")
        if normalized in {"group", "groupchat", "groupconversation"}:
            return "group"
        if normalized in {"onetoone", "onetoonechat", "oneonone"}:
            return "oneOnOne"
        return raw_chat_type

    def _complete_csa_member_count(self, row: dict[str, Any]) -> int | None:
        """Count a roster only when it is versioned, identified, and includes self."""
        roster_version = row.get("rosterVersion")
        if not (
            (isinstance(roster_version, int) and not isinstance(roster_version, bool))
            or (isinstance(roster_version, str) and roster_version.strip())
        ):
            return None
        members = row.get("members")
        own_id = self._profile_identifier(self._spaces_claims.get("oid"))
        if not isinstance(members, list) or not members or not own_id:
            return None

        participant_ids: set[str] = set()
        for member in members:
            if not isinstance(member, dict):
                return None
            member_id = next(
                (
                    identifier
                    for key in ("id", "mri", "objectId", "userId", "userMri")
                    if (identifier := self._profile_identifier(member.get(key)))
                ),
                None,
            )
            if not member_id:
                return None
            participant_ids.add(member_id)
        if own_id not in participant_ids:
            return None
        return len(participant_ids)

    @staticmethod
    def _sync_token_expired(payload: dict[str, Any]) -> bool:
        error = TeamsConnector._mapping(payload.get("error"))
        values = (
            error.get("code"),
            error.get("type"),
            error.get("message"),
            payload.get("errorCode"),
            payload.get("code"),
            payload.get("type"),
        )
        return any(
            isinstance(value, str)
            and "synctokenexpired" in re.sub(r"[^a-z]", "", value.casefold())
            for value in values
        )

    def list_chats(self) -> list[ChatSummary]:
        """Return a normalized chat catalog in Teams-visible activity order."""
        self._require_logged_in()
        logger.info("Teams chat catalog fetch started")
        payload = self._request_csa(self.CSA_STARTUP_URL)
        metadata = self._mapping(payload.get("metadata"))
        is_partial = metadata.get("isPartialData") is True or metadata.get(
            "hasMoreChats"
        ) is True
        csa_reported_complete = (
            metadata.get("isPartialData") is False
            and metadata.get("hasMoreChats") is False
        )
        csa_reported_partial = (
            True if is_partial else False if csa_reported_complete else None
        )
        needs_chat_service_fallback = csa_reported_partial is not False
        chats = payload.get("chats")
        if not isinstance(chats, list):
            raise RuntimeError("Unexpected CSA response: 'chats' is not a list.")

        normalized: list[ChatSummary] = []
        for chat in chats:
            if not isinstance(chat, dict):
                raise RuntimeError("Unexpected CSA response: a chat row is not an object.")
            chat_id = chat.get("id")
            if not isinstance(chat_id, str) or not chat_id:
                raise RuntimeError("Unexpected CSA response: a chat row has no ID.")
            last_message = self._mapping(chat.get("lastMessage"))
            title = (
                chat.get("title")
                or chat.get("displayName")
                or chat.get("topic")
                or chat_id
            )
            content = last_message.get("content")
            preview = content.strip()[:240] if isinstance(content, str) else None
            is_read = self._optional_bool(chat.get("isRead"))
            unread = None if is_read is None else not is_read
            if unread is None:
                unread = self._optional_bool(chat.get("isUnread"))
            chat_type = self._csa_chat_type(chat)
            normalized.append(
                ChatSummary(
                    id=chat_id,
                    display_name=str(title),
                    last_message_at=self._message_datetime(last_message),
                    last_message_preview=preview,
                    unread=unread,
                    chat_type=chat_type,
                    avatar_url=self._avatar_url_from_rows(chat),
                    is_self_chat=chat_id == self.SELF_CHAT_ID,
                )
            )
        self._resolve_untitled_one_to_one_names(
            chats,
            normalized,
            payload.get("users"),
        )
        self._add_one_to_one_profile_picture_uris(chats, normalized)
        fallback_rows: list[dict[str, Any]] = []
        fallback_summaries: list[ChatSummary] = []
        fallback_chats_added = 0
        non_chat_rows_excluded = 0
        unclassified_rows_excluded = 0
        chat_service_unique_rows: int | None = None
        fallback_name_lookups_attempted = 0
        fallback_chats_without_name = 0
        if needs_chat_service_fallback:
            service_rows = self._list_chat_service_conversations()
            chat_service_unique_rows = len(service_rows)
            known_ids = {chat.id for chat in normalized}
            for row in service_rows:
                conversation_id = row.get("id")
                if not isinstance(conversation_id, str) or conversation_id in known_ids:
                    continue
                row_kind = self._chat_service_conversation_kind(row)
                if row_kind == "non_chat":
                    non_chat_rows_excluded += 1
                    continue
                if row_kind != "chat":
                    unclassified_rows_excluded += 1
                    continue
                summary = self._summarize_chat_service_conversation(row)
                normalized.append(summary)
                fallback_rows.append(row)
                fallback_summaries.append(summary)
                fallback_chats_added += 1
                known_ids.add(conversation_id)
            fallback_name_lookups_attempted = self._resolve_fallback_chat_names(
                fallback_rows, normalized
            )
        self_chat_synthesized = not any(
            chat.id == self.SELF_CHAT_ID for chat in normalized
        )
        self._ensure_self_chat(normalized)
        fallback_chats_without_name = sum(
            summary.display_name == summary.id
            for summary in fallback_summaries
        )
        # The visible Teams chat rows matched descending last-message time in a live
        # comparison better than the raw CSA array. Stable sorting retains the
        # CSA order when two chats have the same timestamp.
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        ordered = sorted(
            normalized,
            key=lambda chat: chat.last_message_at or oldest,
            reverse=True,
        )

        self.catalog_coverage = ChatCatalogCoverage(
            catalog_complete=(
                csa_reported_partial is False
                or (
                    chat_service_unique_rows is not None
                    and unclassified_rows_excluded == 0
                )
            ),
            catalog_source=(
                "; ".join(
                    source
                    for source, enabled in (
                        ("CSA startup", True),
                        (
                            "paginated Chat Service fallback",
                            chat_service_unique_rows is not None,
                        ),
                        ("known self-chat correction", self_chat_synthesized),
                    )
                    if enabled
                )
            ),
            catalog_scope=self.CHAT_CATALOG_SCOPE,
            csa_reported_partial=csa_reported_partial,
            chat_service_unique_rows=chat_service_unique_rows,
            fallback_chats_added=fallback_chats_added,
            non_chat_service_rows_excluded=non_chat_rows_excluded,
            unclassified_service_rows_excluded=unclassified_rows_excluded,
            fallback_name_lookups_attempted=fallback_name_lookups_attempted,
            fallback_chats_without_resolved_name=fallback_chats_without_name,
        )
        logger.info(
            "Teams chat catalog fetch completed chats=%d csa_partial=%s fallback_added=%d excluded_non_chat=%d excluded_unclassified=%d",
            len(ordered),
            csa_reported_partial,
            fallback_chats_added,
            non_chat_rows_excluded,
            unclassified_rows_excluded,
        )
        return ordered

    @classmethod
    def _chat_service_conversation_kind(cls, conversation: dict[str, Any]) -> str:
        """Classify Chat Service rows before merging them into the chat catalog."""
        if conversation.get("id") == cls.SELF_CHAT_ID:
            return "chat"
        thread_properties = cls._mapping(conversation.get("threadProperties"))
        raw_types = (
            thread_properties.get("productThreadType"),
            thread_properties.get("threadType"),
        )
        normalized_types = {
            value.casefold().replace("_", "").replace("-", "").replace(" ", "")
            for value in raw_types
            if isinstance(value, str) and value.strip()
        }
        if normalized_types & cls.CHAT_SERVICE_NON_CHAT_TYPES:
            return "non_chat"
        if normalized_types & cls.CHAT_SERVICE_CHAT_TYPES:
            return "chat"
        return "unclassified"

    def _resolve_fallback_chat_names(
        self,
        service_rows: list[dict[str, Any]],
        chats: list[ChatSummary],
    ) -> int:
        """Resolve a bounded number of untitled fallback one-to-one chats."""
        own_id = self._profile_identifier(self._spaces_claims.get("oid"))
        if not own_id:
            return 0
        summaries = {chat.id: chat for chat in chats}
        candidates = []
        for row in service_rows:
            conversation_id = row.get("id")
            summary = summaries.get(conversation_id)
            thread_properties = self._mapping(row.get("threadProperties"))
            product_type = thread_properties.get("productThreadType")
            if (
                summary is not None
                and summary.display_name == summary.id
                and isinstance(product_type, str)
                and product_type.casefold() == "onetoonechat"
            ):
                candidates.append((conversation_id, summary))

        attempted = 0
        for conversation_id, summary in candidates[: self.MAX_FALLBACK_NAME_LOOKUPS]:
            attempted += 1
            try:
                members = self.get_conversation_members(conversation_id)
            except (RuntimeError, OSError):
                continue
            peers: dict[str, str | None] = {}
            for member in members:
                member_id = next(
                    (
                        identifier
                        for key in ("id", "mri", "objectId", "userId", "userMri")
                        if (identifier := self._profile_identifier(member.get(key)))
                    ),
                    None,
                )
                if not member_id or member_id == own_id:
                    continue
                name = next(
                    (
                        value.strip()
                        for key in ("userDisplayName", "displayName", "name")
                        if isinstance((value := member.get(key)), str) and value.strip()
                    ),
                    None,
                )
                peers[member_id] = name
            if len(peers) == 1:
                name = next(iter(peers.values()))
                if name:
                    summary.display_name = name
        return attempted

    def _ensure_self_chat(self, chats: list[ChatSummary]) -> None:
        """Include Teams' special notes-to-self thread in the normalized catalog."""
        chat = next(
            (item for item in chats if item.id == self.SELF_CHAT_ID),
            None,
        )
        if chat is None:
            chat = ChatSummary(
                id=self.SELF_CHAT_ID,
                display_name=self._self_chat_display_name(),
                chat_type="chat",
                is_self_chat=True,
            )
            chats.append(chat)
        chat.is_self_chat = True
        chat.display_name = self._self_chat_display_name()
        chat.chat_type = "chat"
        chat.avatar_url = chat.avatar_url or self.get_profile_picture_uri(
            display_name=self._self_chat_display_name().removesuffix(" (You)")
        )

    def _self_chat_display_name(self) -> str:
        name = self._spaces_claims.get("name") or self._spaces_claims.get(
            "preferred_username"
        )
        if not isinstance(name, str) or not name.strip():
            return "Notes to self"
        label = name.strip()
        if not label.casefold().endswith(" (you)"):
            label += " (You)"
        return label

    def _list_chat_service_conversations(self) -> list[dict[str, Any]]:
        """Follow Chat Service pages to complete a partial CSA catalog."""
        if not self._chat_service_url:
            raise RuntimeError("Teams AuthZ did not return a Chat Service URL.")

        endpoint = (
            f"{self._chat_service_url.rstrip('/')}/v1/users/ME/conversations"
        )
        url = self._set_query_parameters(
            endpoint,
            view="mychats",
            pageSize="500",
        )
        conversations: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        while url:
            self._validate_service_url(url, self._chat_service_url, "Chat Service")
            if url in seen_urls:
                raise RuntimeError(
                    "Teams repeated a conversation catalog continuation link."
                )
            seen_urls.add(url)

            payload = self._request_chat_service(url)
            page = payload.get("conversations")
            if not isinstance(page, list) or any(
                not isinstance(conversation, dict) for conversation in page
            ):
                raise RuntimeError("Unexpected Teams conversation catalog response.")

            for conversation in page:
                conversation_id = conversation.get("id")
                if not isinstance(conversation_id, str) or not conversation_id:
                    raise RuntimeError(
                        "Unexpected Teams conversation catalog row without an ID."
                    )
                if conversation_id in seen_ids:
                    continue
                conversations.append(conversation)
                seen_ids.add(conversation_id)

            metadata = self._mapping(payload.get("_metadata"))
            backward_link = metadata.get("backwardLink")
            if backward_link is not None and not isinstance(backward_link, str):
                raise RuntimeError(
                    "Unexpected Teams conversation catalog continuation link."
                )
            url = backward_link or None

        return conversations

    def _summarize_chat_service_conversation(
        self, conversation: dict[str, Any]
    ) -> ChatSummary:
        conversation_id = str(conversation["id"])
        properties = self._mapping(conversation.get("properties"))
        thread_properties = self._mapping(conversation.get("threadProperties"))
        last_message = self._mapping(conversation.get("lastMessage"))
        title = next(
            (
                value.strip()
                for value in (
                    thread_properties.get("topic"),
                    properties.get("displayName"),
                    conversation.get("title"),
                    conversation.get("displayName"),
                )
                if isinstance(value, str) and value.strip()
            ),
            conversation_id,
        )
        last_message_at = self._message_datetime(last_message) or (
            self._message_datetime(
                {"originalArrivalTime": properties.get("lastimreceivedtime")}
            )
        )
        content = last_message.get("content")
        preview = content.strip()[:240] if isinstance(content, str) else None
        chat_type = (
            thread_properties.get("threadType")
            or thread_properties.get("productThreadType")
        )
        return ChatSummary(
            id=conversation_id,
            display_name=str(title),
            last_message_at=last_message_at,
            last_message_preview=preview,
            chat_type=chat_type if isinstance(chat_type, str) else None,
            avatar_url=self._avatar_url_from_rows(
                conversation, properties, thread_properties
            ),
            is_self_chat=conversation_id == self.SELF_CHAT_ID,
        )

    def _add_one_to_one_profile_picture_uris(
        self,
        raw_chats: list[Any],
        summaries: list[ChatSummary],
    ) -> None:
        own_id = self._profile_identifier(self._spaces_claims.get("oid"))
        if not own_id:
            return
        summaries_by_id = {summary.id: summary for summary in summaries}
        for row in raw_chats:
            if not isinstance(row, dict) or self._optional_bool(row.get("isOneOnOne")) is not True:
                continue
            chat_id = row.get("id")
            summary = summaries_by_id.get(chat_id) if isinstance(chat_id, str) else None
            if summary is None or summary.avatar_url:
                continue
            members = row.get("members")
            if not isinstance(members, list):
                continue
            peers: dict[str, tuple[str, str | None]] = {}
            for member in members:
                if not isinstance(member, dict):
                    continue
                identifiers = [(value, self._profile_identifier(value)) for value in (
                    member.get("mri"), member.get("objectId")
                )]
                name = member.get("displayName") or member.get("name")
                for value, identifier in identifiers:
                    if identifier and identifier != own_id:
                        current = peers.get(identifier)
                        source_id = value if isinstance(value, str) else identifier
                        resolved_name = name.strip() if isinstance(name, str) and name.strip() else None
                        peers[identifier] = (
                            current[0] if current else source_id,
                            resolved_name or (current[1] if current else None),
                        )
            if len(peers) != 1:
                continue
            peer_id, (source_id, member_name) = next(iter(peers.items()))
            display_name = member_name or summary.display_name
            summary.avatar_url = self.get_profile_picture_uri(
                source_id, display_name
            )

    @staticmethod
    def _avatar_url_from_rows(*rows: dict[str, Any]) -> str | None:
        for row in rows:
            for key in ("avatarUrl", "avatarURL", "profilePictureUrl", "imageUri", "imageUrl"):
                value = row.get(key)
                if isinstance(value, dict):
                    value = value.get("webUrl") or value.get("url")
                if not isinstance(value, str) or len(value) > 2048:
                    continue
                candidate = value.strip()
                parsed = urlsplit(candidate)
                if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
                    return candidate
        return None

    def _resolve_untitled_one_to_one_names(
        self,
        raw_chats: list[Any],
        summaries: list[ChatSummary],
        raw_users: Any,
    ) -> None:
        """Use CSA membership and Middle Tier profiles for untitled 1:1 chats."""
        summaries_by_id = {chat.id: chat for chat in summaries}
        own_id = self._profile_identifier(self._spaces_claims.get("oid"))
        names_by_id: dict[str, str] = {}

        users = raw_users if isinstance(raw_users, list) else []
        for user in users:
            if not isinstance(user, dict):
                continue
            name = user.get("displayName") or user.get("userPrincipalName")
            if not isinstance(name, str) or not name.strip():
                continue
            for value in (user.get("objectId"), user.get("mri")):
                identifier = self._profile_identifier(value)
                if identifier:
                    names_by_id[identifier] = name.strip()

        profile_targets: dict[str, list[ChatSummary]] = {}
        for row in raw_chats:
            if not isinstance(row, dict):
                continue
            chat_id = row.get("id")
            summary = summaries_by_id.get(chat_id) if isinstance(chat_id, str) else None
            if summary is None:
                continue
            has_title = any(
                isinstance(row.get(key), str) and row[key].strip()
                for key in ("title", "displayName", "topic")
            )
            if has_title or self._optional_bool(row.get("isOneOnOne")) is not True:
                continue

            member_names: dict[str, str] = {}
            member_ids: list[str] = []
            members = row.get("members")
            if not isinstance(members, list):
                continue
            for member in members:
                if not isinstance(member, dict):
                    continue
                member_ids_for_record = {
                    identifier
                    for identifier in (
                        self._profile_identifier(member.get("objectId")),
                        self._profile_identifier(member.get("mri")),
                    )
                    if identifier
                }
                member_ids.extend(
                    identifier
                    for identifier in member_ids_for_record
                    if identifier not in member_ids
                )
                member_name = member.get("displayName") or member.get("name")
                if isinstance(member_name, str) and member_name.strip():
                    for identifier in member_ids_for_record:
                        member_names[identifier] = member_name.strip()

            peers = [identifier for identifier in member_ids if identifier != own_id]
            if len(peers) != 1:
                continue
            peer_id = peers[0]
            name = member_names.get(peer_id) or names_by_id.get(peer_id)
            if name:
                summary.display_name = name
            else:
                profile_targets.setdefault(peer_id, []).append(summary)

        if not profile_targets:
            return
        try:
            profile_names = self._fetch_middle_tier_profile_names(
                list(profile_targets)
            )
        except RuntimeError:
            # A profile lookup must not make the CSA catalog unavailable.
            return
        for profile_id, summaries_for_profile in profile_targets.items():
            name = profile_names.get(profile_id)
            if name:
                for summary in summaries_for_profile:
                    summary.display_name = name

    def _fetch_middle_tier_profile_names(
        self,
        user_ids: list[str],
    ) -> dict[str, str]:
        if not self._middle_tier_url or not user_ids:
            return {}
        url = (
            f"{self._middle_tier_url.rstrip('/')}/beta/users/fetch"
            "?isMailAddress=false&canBeSmtpAddress=false&enableGuest=true"
            "&includeIBBarredUsers=true&skypeTeamsInfo=true"
        )
        names: dict[str, str] = {}
        for start in range(0, len(user_ids), 100):
            batch = user_ids[start : start + 100]
            payload = self._request_middle_tier(
                url,
                method="POST",
                body=[f"8:orgid:{user_id}" for user_id in batch],
            )
            values = payload.get("value") or payload.get("Value") or []
            if not isinstance(values, list):
                continue
            for profile in values:
                if not isinstance(profile, dict):
                    continue
                name = profile.get("displayName") or profile.get("userPrincipalName")
                if not isinstance(name, str) or not name.strip():
                    continue
                for value in (profile.get("objectId"), profile.get("mri")):
                    identifier = self._profile_identifier(value)
                    if identifier:
                        names[identifier] = name.strip()
        return names

    @staticmethod
    def _profile_identifier(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        identifier = value.strip()
        if identifier.lower().startswith("8:orgid:"):
            identifier = identifier[len("8:orgid:") :]
        return identifier.lower() or None

    @classmethod
    def _message_author_id(cls, message: dict[str, Any]) -> str | None:
        """Return a canonical sender MRI/object ID when the message exposes one."""
        id_keys = ("id", "mri", "objectId", "userId", "userMri")
        sender_keys = (
            "from",
            "fromId",
            "fromMri",
            "sender",
            "senderId",
            "senderMri",
            "authorId",
            "authorMri",
            "userId",
            "userMri",
        )
        for key in sender_keys:
            value = message.get(key)
            candidates = (
                [value.get(nested_key) for nested_key in id_keys]
                if isinstance(value, dict)
                else [value]
            )
            for candidate in candidates:
                if not isinstance(candidate, str) or not candidate.strip():
                    continue
                normalized = candidate.strip()
                mri_match = cls._MESSAGE_AUTHOR_MRI.search(normalized)
                if mri_match:
                    return mri_match.group(1).lower()
                if cls._OBJECT_ID.fullmatch(normalized):
                    return f"8:orgid:{normalized.lower()}"
        return None

    def get_messages(
        self,
        conversation_id: str,
        limit: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cancel_event: Event | None = None,
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Read matching history and return it oldest to newest.

        Date bounds are inclusive. ``since`` is also sent as the observed
        server-side ``startTime`` lower bound; local checks remain authoritative.
        ``until`` stays local because server-side ``endTime`` behavior is
        inconsistent. Segment metadata is not used as an early-stop condition.
        """
        messages = [
            message
            for page in self.iter_message_pages(
                conversation_id,
                limit=limit,
                since=since,
                until=until,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
            for message in page
        ]
        return sorted(messages, key=self._message_order_key)

    def iter_message_pages(
        self,
        conversation_id: str,
        limit: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cancel_event: Event | None = None,
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield deduplicated chronological pages while traversing newest first.

        Each yielded page is oldest to newest, while successive pages move from
        newer history to older history. Exporters can spool pages and replay
        them in reverse without retaining every message body in memory.
        """
        self._require_logged_in()
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string.")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer or None.")
        since_utc = self._validate_bound("since", since)
        until_utc = self._validate_bound("until", until)
        if since_utc is not None and until_utc is not None and since_utc > until_utc:
            raise ValueError("since must be earlier than or equal to until.")

        endpoint = self._messages_url(conversation_id)
        first_page_size = min(
            self.MESSAGE_PAGE_SIZE,
            limit if limit is not None else self.MESSAGE_PAGE_SIZE,
        )
        url = self._set_query_parameters(
            endpoint,
            startTime=(
                str(self._epoch_milliseconds(since_utc))
                if since_utc is not None
                else "0"
            ),
            pageSize=str(first_page_size),
            view="msnp24Equivalent",
        )

        seen_message_ids: set[str] = set()
        seen_urls: set[str] = set()
        pages_without_new_messages = 0
        pages_fetched = 0
        raw_items_scanned = 0
        messages_retrieved = 0
        duplicate_messages_skipped = 0
        missing_timestamp_messages_excluded = 0
        retry_count = 0
        limit_reached = False
        history_complete = False
        self.last_message_read_coverage = {
            "complete": False,
            "pages_fetched": 0,
            "raw_items_scanned": 0,
            "messages_retrieved": 0,
            "duplicate_messages_skipped": 0,
            "missing_timestamp_messages_excluded": 0,
            "retry_count": 0,
            "limit_reached": False,
            "since": since_utc.isoformat().replace("+00:00", "Z") if since_utc else None,
            "until": until_utc.isoformat().replace("+00:00", "Z") if until_utc else None,
        }

        try:
            while url:
                self._raise_if_cancelled(cancel_event)
                self._validate_service_url(url, self._chat_service_url, "Chat Service")
                if url in seen_urls:
                    raise RuntimeError("Teams repeated a message continuation link.")
                seen_urls.add(url)
                try:
                    payload = self._request_chat_service(
                        url,
                        cancel_event=cancel_event,
                    )
                except Exception:
                    retry_count += self.last_request_retry_count
                    if progress_callback is not None:
                        progress_callback(
                            pages_fetched, messages_retrieved, retry_count
                        )
                    raise
                retry_count += self.last_request_retry_count

                page = payload.get("messages")
                if not isinstance(page, list) or any(
                    not isinstance(message, dict) for message in page
                ):
                    raise RuntimeError("Unexpected Teams messages response.")

                raw_items_scanned += len(page)
                new_ids_on_page = 0
                selected_descending: list[dict[str, Any]] = []
                for message in sorted(page, key=self._message_order_key, reverse=True):
                    message_id = message.get("id")
                    if message_id is not None:
                        message_id = str(message_id)
                        if message_id in seen_message_ids:
                            duplicate_messages_skipped += 1
                            continue
                        seen_message_ids.add(message_id)
                        new_ids_on_page += 1

                    timestamp = self._message_datetime(message)
                    if (since_utc is not None or until_utc is not None) and timestamp is None:
                        missing_timestamp_messages_excluded += 1
                        continue
                    if since_utc is not None and timestamp < since_utc:
                        continue
                    if until_utc is not None and timestamp > until_utc:
                        continue
                    selected_descending.append(message)
                    messages_retrieved += 1
                    if limit is not None and messages_retrieved >= limit:
                        limit_reached = True
                        break

                pages_fetched += 1
                logger.debug(
                    "Teams message page fetched page=%d scanned=%d retrieved_total=%d retries_total=%d",
                    pages_fetched,
                    len(page),
                    messages_retrieved,
                    retry_count,
                )
                metadata = self._mapping(payload.get("_metadata"))
                backward_link = metadata.get("backwardLink")
                if backward_link is not None and not isinstance(backward_link, str):
                    raise RuntimeError("Unexpected Teams message continuation link.")
                next_url = backward_link or None
                if next_url:
                    self._validate_service_url(
                        next_url,
                        self._chat_service_url,
                        "Chat Service",
                    )

                pages_without_new_messages = (
                    0 if new_ids_on_page else pages_without_new_messages + 1
                )
                if pages_without_new_messages >= 3 and not limit_reached:
                    raise RuntimeError(
                        "Teams returned three message pages without new message IDs."
                    )
                if progress_callback is not None:
                    progress_callback(pages_fetched, messages_retrieved, retry_count)
                if selected_descending:
                    yield list(reversed(selected_descending))

                if limit_reached or next_url is None:
                    history_complete = next_url is None
                    break
                if next_url in seen_urls:
                    raise RuntimeError("Teams repeated a message continuation link.")
                url = next_url

            self.last_message_read_coverage["complete"] = history_complete
        finally:
            self.last_message_read_coverage.update(
                {
                    "pages_fetched": pages_fetched,
                    "raw_items_scanned": raw_items_scanned,
                    "messages_retrieved": messages_retrieved,
                    "duplicate_messages_skipped": duplicate_messages_skipped,
                    "missing_timestamp_messages_excluded": missing_timestamp_messages_excluded,
                    "retry_count": retry_count,
                    "limit_reached": limit_reached,
                }
            )
            logger.info(
                "Teams history read finished pages=%d scanned=%d retrieved=%d duplicates=%d retries=%d complete=%s",
                pages_fetched,
                raw_items_scanned,
                messages_retrieved,
                duplicate_messages_skipped,
                retry_count,
                history_complete,
            )

    @staticmethod
    def _raise_if_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("Extraction was cancelled by the caller.")

    def get_messages_page(
        self,
        conversation_id: str,
        page_size: int = 50,
        since: datetime | None = None,
        until: datetime | None = None,
        continuation_url: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, int, int]:
        """Read one bounded service page for resumable extraction.

        ``continuation_url`` is an opaque service link and must be passed back
        unchanged. Its origin and conversation path are validated before use.
        The returned counts are raw items scanned and items omitted because a
        requested time filter could not be checked due to a missing timestamp.
        """
        self._require_logged_in()
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string.")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100.")
        since_utc = self._validate_bound("since", since)
        until_utc = self._validate_bound("until", until)
        if since_utc is not None and until_utc is not None and since_utc > until_utc:
            raise ValueError("since must be earlier than or equal to until.")

        if continuation_url is None:
            url = self._set_query_parameters(
                self._messages_url(conversation_id),
                startTime=(
                    str(self._epoch_milliseconds(since_utc))
                    if since_utc is not None
                    else "0"
                ),
                pageSize=str(page_size),
                view="msnp24Equivalent",
            )
        else:
            url = continuation_url
        self._validate_message_page_url(url, conversation_id)

        logger.debug(
            "Teams bounded message page request started page_size=%d has_since=%s has_until=%s continuation=%s",
            page_size,
            since_utc is not None,
            until_utc is not None,
            continuation_url is not None,
        )
        payload = self._request_chat_service(url)
        page = payload.get("messages")
        if not isinstance(page, list) or any(
            not isinstance(message, dict) for message in page
        ):
            raise RuntimeError("Unexpected Teams messages response.")

        selected: list[dict[str, Any]] = []
        missing_timestamp_count = 0
        for message in page:
            timestamp = self._message_datetime(message)
            if (since_utc is not None or until_utc is not None) and timestamp is None:
                missing_timestamp_count += 1
                continue
            if since_utc is not None and timestamp < since_utc:
                continue
            if until_utc is not None and timestamp > until_utc:
                continue
            selected.append(message)

        metadata = self._mapping(payload.get("_metadata"))
        next_url = metadata.get("backwardLink")
        if next_url is not None and not isinstance(next_url, str):
            raise RuntimeError("Unexpected Teams message continuation link.")
        if next_url:
            self._validate_message_page_url(next_url, conversation_id)
        logger.debug(
            "Teams bounded message page received scanned=%d selected=%d missing_timestamps=%d has_continuation=%s",
            len(page),
            len(selected),
            missing_timestamp_count,
            bool(next_url),
        )
        return (
            sorted(selected, key=self._message_order_key),
            next_url or None,
            len(page),
            missing_timestamp_count,
        )

    def get_conversation_members(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return the service roster for a conversation, when exposed."""
        self._require_logged_in()
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string.")
        if not self._chat_service_url:
            raise RuntimeError("Teams AuthZ did not return a Chat Service URL.")
        endpoint = (
            f"{self._chat_service_url.rstrip('/')}/v1/threads/"
            f"{quote(conversation_id, safe='')}/members"
        )
        payload = self._request_chat_service(endpoint)
        members = payload.get("members")
        if not isinstance(members, list) or any(
            not isinstance(member, dict) for member in members
        ):
            raise RuntimeError("Unexpected Teams conversation members response.")
        resolved_members = [dict(member) for member in members]
        identifiers = {
            identifier
            for member in resolved_members
            for key in ("id", "mri", "objectId", "userId", "userMri")
            if (identifier := self._profile_identifier(member.get(key)))
        }
        if identifiers:
            try:
                profile_names = self._fetch_middle_tier_profile_names(sorted(identifiers))
            except RuntimeError:
                profile_names = {}
            for member in resolved_members:
                existing_name = member.get("userDisplayName") or member.get("displayName")
                if isinstance(existing_name, str) and existing_name.strip():
                    continue
                for key in ("id", "mri", "objectId", "userId", "userMri"):
                    identifier = self._profile_identifier(member.get(key))
                    name = profile_names.get(identifier) if identifier else None
                    if name:
                        member["displayName"] = name
                        break
        return resolved_members

    def send_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        """Send one plain-text message through the Teams Chat Service.

        The caller must obtain explicit user authorization before calling this
        method. This method makes one POST attempt and never retries, because a
        network failure can leave delivery status uncertain.
        """
        self._require_logged_in()
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string.")
        if len(text) > 10_000:
            raise ValueError("text must contain at most 10,000 characters.")
        if "\x00" in text:
            raise ValueError("text cannot contain a null character.")

        now = datetime.now(timezone.utc)
        candidate_id = self._epoch_milliseconds(now)
        self._last_client_message_id = max(
            candidate_id, self._last_client_message_id + 1
        )
        client_message_id = str(self._last_client_message_id)
        normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
        escaped_lines = [
            html.escape(line, quote=False) for line in normalized_text.split("\n")
        ]
        content = "<p>" + "<br>".join(escaped_lines) + "</p>"
        sender_name = (
            self._spaces_claims.get("name")
            or self._spaces_claims.get("preferred_username")
            or ""
        )
        if not isinstance(sender_name, str):
            sender_name = ""
        payload = {
            "content": content,
            "messagetype": "RichText/Html",
            "contenttype": "text",
            "clientmessageid": client_message_id,
            "imdisplayname": sender_name,
            "properties": {"importance": "", "subject": None},
        }
        logger.info("Teams message send started characters=%d", len(normalized_text))
        response = self._request_chat_service(
            self._messages_url(conversation_id.strip()),
            method="POST",
            body=payload,
        )
        if response.get("error") or response.get("isError") is True:
            raise RuntimeError("Teams rejected the message send request.")

        response_message = response.get("message")
        if not isinstance(response_message, dict):
            response_message = response
        message_id = next(
            (
                str(response_message[key])
                for key in ("id", "Id", "messageId", "MessageId")
                if response_message.get(key) is not None
            ),
            None,
        )
        sent_at = self._message_datetime(response_message)
        logger.info("Teams message send accepted response_has_message_id=%s", message_id is not None)
        return {
            "message_id": message_id,
            "client_message_id": client_message_id,
            "sent_at": (
                sent_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if sent_at
                else None
            ),
        }

    @staticmethod
    def _validate_bound(name: str, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"{name} must be a datetime or None.")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone.")
        return value.astimezone(timezone.utc)

    def _validate_message_page_url(self, url: str, conversation_id: str) -> None:
        if not self._chat_service_url:
            raise RuntimeError("Teams AuthZ did not return a Chat Service URL.")
        self._validate_service_url(url, self._chat_service_url, "Chat Service")
        expected_path = urlsplit(self._messages_url(conversation_id)).path
        actual = urlsplit(url)
        from urllib.parse import unquote

        if (
            actual.username
            or actual.password
            or actual.fragment
            or unquote(actual.path) != unquote(expected_path)
        ):
            raise RuntimeError("Refusing a continuation URL for another Teams resource.")

    def _messages_url(self, conversation_id: str) -> str:
        if not self._chat_service_url:
            raise RuntimeError("Teams AuthZ did not return a Chat Service URL.")
        encoded_id = quote(conversation_id, safe="")
        return (
            f"{self._chat_service_url.rstrip('/')}/v1/users/ME/conversations/"
            f"{encoded_id}/messages"
        )

    def _request_chat_service(
        self,
        url: str,
        method: str = "GET",
        body: Any | None = None,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        self._require_logged_in()
        self._validate_service_url(url, self._chat_service_url, "Chat Service")
        return self._http_json(
            url,
            method=method,
            body=body,
            headers={
                "Authentication": f"skypetoken={self._skype_token}",
                "Accept": "application/json",
            },
            cancel_event=cancel_event,
        )

    def _request_middle_tier(
        self,
        url: str,
        method: str = "GET",
        body: Any | None = None,
    ) -> dict[str, Any]:
        self._require_logged_in()
        self._validate_service_url(url, self._middle_tier_url, "Middle Tier")
        return self._http_json(
            url,
            method=method,
            body=body,
            headers={
                "Authorization": f"Bearer {self._spaces_access_token}",
                "Accept": "application/json",
            },
        )

    def _request_csa(
        self,
        url: str,
        method: str = "GET",
        body: Any | None = None,
        sync_token: str | None = None,
    ) -> dict[str, Any]:
        self._require_logged_in()
        host = urlsplit(url)
        if host.scheme != "https" or host.netloc.lower() != "teams.microsoft.com":
            raise RuntimeError("Refusing a CSA request outside teams.microsoft.com.")
        headers = {
            "Authorization": f"Bearer {self._csa_access_token}",
            "x-skypetoken": str(self._skype_token),
            "Accept": "application/json",
        }
        if sync_token is not None:
            headers["x-ms-synctoken"] = sync_token
        return self._http_json(
            url,
            method=method,
            body=body,
            headers=headers,
        )

    def _request_teams_authz(self) -> dict[str, Any]:
        if not self._spaces_access_token:
            raise RuntimeError("Teams spaces token is unavailable.")
        return self._http_json(
            self.AUTHZ_URL,
            method="POST",
            body=b"",
            headers={
                "Authorization": f"Bearer {self._spaces_access_token}",
                "Accept": "application/json; ver=1.0",
            },
        )

    def _http_json(
        self,
        url: str,
        method: str = "GET",
        body: Any | None = None,
        headers: dict[str, str] | None = None,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        """Send JSON with bounded retries for idempotent read requests only."""
        data: bytes | None
        request_headers = dict(headers or {})
        if isinstance(body, bytes):
            data = body
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        else:
            data = None
        request_headers.setdefault("User-Agent", "Microsoft Teams compatible client")
        read_request = method.upper() in {"GET", "HEAD"}
        max_attempts = self.READ_REQUEST_MAX_ATTEMPTS if read_request else 1
        self.last_request_retry_count = 0
        transient_error: _TransientTeamsRequestError | None = None

        for attempt in range(1, max_attempts + 1):
            self._raise_if_cancelled(cancel_event)
            attempt_started = time.perf_counter()
            failure_kind = "request"
            failure_status: int | None = None
            request = urllib.request.Request(
                url, data=data, method=method, headers=request_headers
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.REQUEST_TIMEOUT_SECONDS
                ) as response:
                    response_status = response.status
                    response_text = response.read().decode("utf-8")
                self._raise_if_cancelled(cancel_event)
                logger.debug(
                    "Teams request completed method=%s status=%d attempt=%d duration_ms=%.1f",
                    method.upper(),
                    response_status,
                    attempt,
                    (time.perf_counter() - attempt_started) * 1000,
                )
                break
            except urllib.error.HTTPError as exc:
                location = self._safe_location(url)
                if exc.code in {401, 403}:
                    logger.warning(
                        "Teams request rejected stored credentials method=%s status=%d",
                        method.upper(),
                        exc.code,
                    )
                    exc.close()
                    self._clear_session()
                    raise AuthenticationRequiredError(
                        "Teams no longer accepts the stored credentials for this request. "
                        "Run `python -m teams_exporter auth` and retry."
                    ) from None
                if read_request and exc.code in self.RETRYABLE_HTTP_STATUSES:
                    failure_kind = "http"
                    failure_status = exc.code
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    transient_error = _TransientTeamsRequestError(
                        f"Teams request failed with HTTP {exc.code} for {location}.",
                        retry_after,
                    )
                    exc.close()
                else:
                    error = _TeamsHttpError(
                        f"Teams request failed with HTTP {exc.code} for {location}.",
                        exc.code,
                    )
                    exc.close()
                    raise error from exc
            except urllib.error.URLError as exc:
                failure_kind = "network"
                location = self._safe_location(url)
                transient_error = _TransientTeamsRequestError(
                    f"Teams request failed for {location}: {self._safe_exception_detail(exc.reason)}"
                )
            except (http.client.IncompleteRead, TimeoutError, OSError) as exc:
                failure_kind = "network"
                location = self._safe_location(url)
                transient_error = _TransientTeamsRequestError(
                    f"Teams request failed for {location}: {self._safe_exception_detail(exc)}"
                )
            except Exception as exc:
                failure_kind = "request"
                location = self._safe_location(url)
                if not read_request:
                    raise RuntimeError(
                        f"Teams request failed for {location}: "
                        f"{self._safe_exception_detail(exc)}"
                    ) from None
                transient_error = _TransientTeamsRequestError(
                    f"Teams request failed for {location}: {self._safe_exception_detail(exc)}"
                )

            if transient_error is None:
                raise RuntimeError("Teams request failed without an error detail.")
            if attempt >= max_attempts:
                logger.error(
                    "Teams request exhausted retries method=%s failure=%s status=%s attempts=%d",
                    method.upper(),
                    failure_kind,
                    failure_status if failure_status is not None else "none",
                    attempt,
                )
                raise RuntimeError(
                    f"{transient_error} Read request exhausted its {max_attempts}-attempt limit."
                ) from transient_error

            self.last_request_retry_count += 1
            delay = self._read_retry_delay(
                self.last_request_retry_count,
                transient_error.retry_after,
            )
            logger.warning(
                "Teams request retry method=%s failure=%s status=%s attempt=%d/%d delay_ms=%.0f",
                method.upper(),
                failure_kind,
                failure_status if failure_status is not None else "none",
                attempt,
                max_attempts,
                delay * 1000,
            )
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    raise ExtractionCancelled("Extraction was cancelled during retry backoff.")
            else:
                time.sleep(delay)
            transient_error = None
        else:
            raise RuntimeError("Teams request failed without a response.")

        if not response_text.strip():
            payload = {}
        else:
            try:
                payload = json.loads(response_text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Teams returned a non-JSON response for {self._safe_location(url)}."
                ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Teams returned a JSON value that was not an object.")
        return payload

    @classmethod
    def _read_retry_delay(cls, retry_number: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = -1
            if delay >= 0:
                return min(delay, cls.READ_RETRY_AFTER_MAX_SECONDS)
        return min(
            cls.READ_RETRY_BASE_SECONDS * (2 ** max(0, retry_number - 1)),
            cls.READ_RETRY_MAX_SECONDS,
        )

    @staticmethod
    def _safe_exception_detail(value: Any) -> str:
        detail = str(value).strip()
        if not detail:
            return "unknown network error"
        detail = re.sub(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*",
            "Bearer [redacted]",
            detail,
        )
        detail = re.sub(
            r"(?i)\b(skypetoken|x-skypetoken|access_token|refresh_token|id_token|authorization|cookie|set-cookie|client_secret)\s*[:=]\s*[^&\s,;]+",
            r"\1=[redacted]",
            detail,
        )
        detail = re.sub(
            r"https?://[^\s'\"]+",
            lambda match: TeamsConnector._redact_url(match.group(0)),
            detail,
        )
        return detail[:300]

    @staticmethod
    def _redact_url(value: str) -> str:
        parts = urlsplit(value.rstrip(".,);"))
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}{parts.path}"

    def _require_logged_in(self) -> None:
        if not self.has_active_session:
            raise RuntimeError("Call TeamsConnector.login() before using the connector.")

    def _clear_session(self) -> None:
        self._spaces_access_token = None
        self._csa_access_token = None
        self._skype_token = None
        self._spaces_claims = {}
        self._authz_response = {}
        self._chat_service_url = None
        self._middle_tier_url = None
        self._auth_sources = {}

    def invalidate_session(self) -> None:
        """Forget the in-memory session after credentials are rejected."""
        self._clear_session()

    def _validate_service_url(
        self,
        url: str,
        service_base: str | None,
        service_name: str,
    ) -> None:
        expected = urlsplit(service_base or "")
        actual = urlsplit(url)
        if (
            actual.scheme != "https"
            or not expected.netloc
            or actual.netloc.lower() != expected.netloc.lower()
        ):
            raise RuntimeError(f"Refusing a URL outside the configured {service_name}.")

    @staticmethod
    def _safe_location(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.netloc}{parts.path}"

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _optional_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True
            if lowered in {"false", "0", "no"}:
                return False
        return None

    @classmethod
    def _message_datetime(cls, message: dict[str, Any]) -> datetime | None:
        for key in (
            "OriginalArrivalTime",
            "originalArrivalTime",
            "originalarrivaltime",
            "composeTime",
            "composetime",
        ):
            value = message.get(key)
            if isinstance(value, datetime):
                parsed = value
            elif isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                except ValueError:
                    continue
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                seconds = value / 1000 if abs(value) > 100_000_000_000 else value
                try:
                    return datetime.fromtimestamp(seconds, tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    continue
            else:
                continue
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        return None

    @staticmethod
    def _epoch_milliseconds(value: datetime) -> int:
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = value - epoch
        return (delta.days * 86_400 + delta.seconds) * 1_000 + delta.microseconds // 1_000

    @classmethod
    def _message_order_key(
        cls, message: dict[str, Any]
    ) -> tuple[int, int, datetime, str]:
        try:
            sequence = int(message.get("sequenceId"))
            has_sequence = 1
        except (TypeError, ValueError):
            sequence = 0
            has_sequence = 0
        timestamp = cls._message_datetime(message) or datetime.min.replace(
            tzinfo=timezone.utc
        )
        return (
            0 if has_sequence else 1,
            sequence,
            timestamp,
            str(message.get("id") or ""),
        )

    @staticmethod
    def _set_query_parameters(url: str, **updates: str) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(updates)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict[str, Any]:
        import base64

        parts = token.split(".")
        if len(parts) < 2:
            return {}
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
            payload = json.loads(decoded.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}
