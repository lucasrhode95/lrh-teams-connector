from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import msal


@dataclass(slots=True)
class ChatSummary:
    """Small, normalized view of a chat returned by Teams."""

    id: str
    display_name: str
    last_message_at: datetime | None = None
    last_message_preview: str | None = None
    unread: bool | None = None
    chat_type: str | None = None


class TeamsConnector:
    """Read chat catalogs and message history through Teams service APIs."""

    TEAMS_CLIENT_ID = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"
    SPACES_SCOPE = "https://api.spaces.skype.com/.default"
    CSA_SCOPE = "https://chatsvcagg.teams.microsoft.com/.default"
    AUTHZ_URL = "https://authsvc.teams.microsoft.com/v1.0/authz"
    CSA_STARTUP_URL = (
        "https://teams.microsoft.com/api/csa/api/v1/teams/users/me"
        "?isPrefetch=false&enableMembershipSummary=true"
    )
    MESSAGE_PAGE_SIZE = 100
    REQUEST_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        tenant: str = "organizations",
        cache_path: str | Path = ".teams_token_cache.json",
    ) -> None:
        self._authority = f"https://login.microsoftonline.com/{tenant}"
        self._cache_path = Path(cache_path)
        self._spaces_access_token: str | None = None
        self._csa_access_token: str | None = None
        self._skype_token: str | None = None
        self._spaces_claims: dict[str, Any] = {}
        self._authz_response: dict[str, Any] = {}
        self._chat_service_url: str | None = None
        self._middle_tier_url: str | None = None
        self._auth_sources: dict[str, str] = {}

    def login(self) -> None:
        """Restore this application's session or use Microsoft device-code login."""
        cache = self._load_token_cache()
        app = msal.PublicClientApplication(
            client_id=self.TEAMS_CLIENT_ID,
            authority=self._authority,
            token_cache=cache,
        )

        spaces_result, spaces_source = self._acquire_resource_token(
            app, self.SPACES_SCOPE
        )
        csa_result, csa_source = self._acquire_resource_token(app, self.CSA_SCOPE)
        self._auth_sources = {
            "spaces": spaces_source,
            "csa": csa_source,
        }
        self._persist_token_cache(cache)

        self._spaces_access_token = spaces_result["access_token"]
        self._csa_access_token = csa_result["access_token"]
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

    def list_chats(self) -> list[ChatSummary]:
        """Return a normalized chat catalog in Teams-visible activity order."""
        self._require_logged_in()
        payload = self._request_csa(self.CSA_STARTUP_URL)
        metadata = self._mapping(payload.get("metadata"))
        if metadata.get("isPartialData") is True or metadata.get("hasMoreChats") is True:
            raise RuntimeError(
                "CSA returned a partial chat catalog; additional synchronization is required."
            )
        chats = payload.get("chats")
        if not isinstance(chats, list):
            raise RuntimeError("Unexpected CSA response: 'chats' is not a list.")

        normalized: list[ChatSummary] = []
        for chat in chats:
            if not isinstance(chat, dict):
                continue
            chat_id = chat.get("id")
            if not isinstance(chat_id, str) or not chat_id:
                continue
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
            chat_type = chat.get("chatType")
            if not isinstance(chat_type, str) or not chat_type:
                if chat.get("isOneOnOne") is True:
                    chat_type = "oneOnOne"
                else:
                    chat_type = None
            normalized.append(
                ChatSummary(
                    id=chat_id,
                    display_name=str(title),
                    last_message_at=self._message_datetime(last_message),
                    last_message_preview=preview,
                    unread=unread,
                    chat_type=chat_type,
                )
            )
        self._resolve_untitled_one_to_one_names(
            chats,
            normalized,
            payload.get("users"),
        )
        # The visible Teams chat rows matched descending last-message time in a live
        # comparison better than the raw CSA array. Stable sorting retains the
        # CSA order when two chats have the same timestamp.
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(
            normalized,
            key=lambda chat: chat.last_message_at or oldest,
            reverse=True,
        )

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

    def get_messages(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Read messages for a chat ID and return them oldest to newest.

        The service returns history through opaque ``backwardLink`` URLs. Date
        filtering is inclusive. Date-bounded requests keep paging to the end of
        the history unless the requested count is satisfied; segment metadata
        is not used as a stopping condition until its semantics are verified.
        """
        self._require_logged_in()
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string.")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer or None.")
        if since is not None and not isinstance(since, datetime):
            raise TypeError("since must be a datetime or None.")
        if since is not None and (
            since.tzinfo is None or since.utcoffset() is None
        ):
            raise ValueError("since must include a timezone.")
        since_utc = since.astimezone(timezone.utc) if since else None

        endpoint = self._messages_url(conversation_id)
        first_page_size = min(
            self.MESSAGE_PAGE_SIZE,
            limit if limit is not None else self.MESSAGE_PAGE_SIZE,
        )
        url = self._set_query_parameters(
            endpoint,
            startTime="0",
            pageSize=str(first_page_size),
            view="msnp24Equivalent",
        )

        results: list[dict[str, Any]] = []
        seen_message_ids: set[str] = set()
        seen_urls: set[str] = set()
        pages_without_new_messages = 0

        while url:
            self._validate_service_url(url, self._chat_service_url, "Chat Service")
            if url in seen_urls:
                raise RuntimeError("Teams repeated a message continuation link.")
            seen_urls.add(url)

            payload = self._request_chat_service(url)
            page = payload.get("messages")
            if not isinstance(page, list) or any(
                not isinstance(message, dict) for message in page
            ):
                raise RuntimeError("Unexpected Teams messages response.")

            new_ids_on_page = 0
            for message in sorted(page, key=self._message_order_key, reverse=True):
                message_id = message.get("id")
                if message_id is not None:
                    message_id = str(message_id)
                    if message_id in seen_message_ids:
                        continue
                    seen_message_ids.add(message_id)
                    new_ids_on_page += 1

                timestamp = self._message_datetime(message)
                if since_utc is not None:
                    if timestamp is None or timestamp < since_utc:
                        continue
                results.append(message)

            if limit is not None and len(results) >= limit:
                results = results[:limit]
                break

            pages_without_new_messages = (
                0 if new_ids_on_page else pages_without_new_messages + 1
            )
            if pages_without_new_messages >= 3:
                raise RuntimeError(
                    "Teams returned three message pages without new message IDs."
                )

            metadata = self._mapping(payload.get("_metadata"))
            backward_link = metadata.get("backwardLink")
            if backward_link is not None and not isinstance(backward_link, str):
                raise RuntimeError("Unexpected Teams message continuation link.")
            url = backward_link or None

        return sorted(results, key=self._message_order_key)

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
    ) -> dict[str, Any]:
        self._require_logged_in()
        host = urlsplit(url)
        if host.scheme != "https" or host.netloc.lower() != "teams.microsoft.com":
            raise RuntimeError("Refusing a CSA request outside teams.microsoft.com.")
        return self._http_json(
            url,
            method=method,
            body=body,
            headers={
                "Authorization": f"Bearer {self._csa_access_token}",
                "x-skypetoken": str(self._skype_token),
                "Accept": "application/json",
            },
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
        *,
        method: str = "GET",
        body: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send JSON without selecting credentials; callers choose service auth."""
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
        request = urllib.request.Request(
            url, data=data, method=method, headers=request_headers
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.REQUEST_TIMEOUT_SECONDS
            ) as response:
                response_text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            location = self._safe_location(url)
            raise RuntimeError(
                f"Teams request failed with HTTP {exc.code} for {location}."
            ) from exc
        except urllib.error.URLError as exc:
            location = self._safe_location(url)
            raise RuntimeError(
                f"Teams request failed for {location}: {exc.reason}"
            ) from exc
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Teams returned a non-JSON response for {self._safe_location(url)}."
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Teams returned a JSON value that was not an object.")
        return payload

    def _acquire_resource_token(
        self,
        app: msal.PublicClientApplication,
        scope: str,
    ) -> tuple[dict[str, Any], str]:
        for account in app.get_accounts():
            result = app.acquire_token_silent(scopes=[scope], account=account)
            if result and result.get("access_token"):
                return result, "cache"

        flow = app.initiate_device_flow(scopes=[scope])
        if not isinstance(flow, dict) or not flow.get("user_code"):
            raise RuntimeError(
                f"Could not start device-code authentication for {self._scope_name(scope)}."
            )
        print(flow.get("message", "Complete sign-in in the browser window."))
        result = app.acquire_token_by_device_flow(flow)
        if not result or not result.get("access_token"):
            error = result.get("error", "authentication_failed") if result else "authentication_failed"
            raise RuntimeError(
                f"Microsoft sign-in failed for {self._scope_name(scope)} ({error})."
            )
        return result, "device_code"

    @staticmethod
    def _scope_name(scope: str) -> str:
        return "CSA" if "chatsvcagg" in scope else "Teams"

    def _load_token_cache(self) -> msal.SerializableTokenCache:
        cache = msal.SerializableTokenCache()
        if not self._cache_path.exists():
            return cache
        try:
            cache.deserialize(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Could not read this application's MSAL cache: {exc.__class__.__name__}."
            ) from exc
        return cache

    def _persist_token_cache(self, cache: msal.SerializableTokenCache) -> None:
        if not cache.has_state_changed:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._cache_path.with_suffix(
            self._cache_path.suffix + ".tmp"
        )
        try:
            temporary_path.write_text(cache.serialize(), encoding="utf-8")
            if os.name != "nt":
                os.chmod(temporary_path, 0o600)
            temporary_path.replace(self._cache_path)
        except OSError as exc:
            raise RuntimeError(
                f"Could not save this application's MSAL cache: {exc.__class__.__name__}."
            ) from exc
        finally:
            if temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _require_logged_in(self) -> None:
        if not (
            self._spaces_access_token
            and self._csa_access_token
            and self._skype_token
        ):
            raise RuntimeError("Call TeamsConnector.login() before using the connector.")

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
            "originalArrivalTime", "originalarrivaltime", "composeTime", "composetime"
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
