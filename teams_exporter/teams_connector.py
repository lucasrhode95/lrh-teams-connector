import base64
import json
import os
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import msal

from teams_exporter.teams_html_to_text import TeamsHtmlToText


class TeamsConnector:
    """Minimal Teams connector with persistent Microsoft authentication."""

    TEAMS_CLIENT_ID = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"
    TEAMS_SCOPE = "https://api.spaces.skype.com/.default"
    AUTHZ_URL = "https://authsvc.teams.microsoft.com/v1.0/authz"

    def __init__(
        self,
        tenant: str = "organizations",
        cache_path: str | Path = ".teams_token_cache.json",
    ) -> None:
        self._authority = f"https://login.microsoftonline.com/{tenant}"
        self._cache_path = Path(cache_path)

        self._access_token: str | None = None
        self._skype_token: str | None = None
        self._access_claims: dict[str, Any] = {}
        self._authz_response: dict[str, Any] = {}
        self._auth_source: str | None = None

        # Lazily populated conversation snapshot. Once a page has been loaded,
        # it stays fixed for the lifetime of this connector instance so calls
        # such as 1-10 followed by 11-20 do not refetch/reorder the first page.
        self._conversation_snapshot: list[dict[str, Any]] = []
        self._conversation_ids: set[str] = set()
        self._conversation_backward_link: str | None = None
        self._conversation_snapshot_started = False
        self._conversation_snapshot_complete = False

        self._user_profile_cache: dict[str, dict[str, Any]] = {}

    def connect(self) -> None:
        """
        Connect to Teams.

        Authentication order:
        1. Load the local MSAL token cache.
        2. Try silent authentication (cached access token / refresh token).
        3. Fall back to device-code login if user interaction is required.
        4. Exchange the Teams-native access token for a Skype token.
        """
        cache = self._load_token_cache()

        app = msal.PublicClientApplication(
            client_id=self.TEAMS_CLIENT_ID,
            authority=self._authority,
            token_cache=cache,
        )

        result = self._acquire_token_silently(app)

        if result and "access_token" in result:
            self._auth_source = "cache"
            print("Using persisted Microsoft authentication.")
        else:
            result = self._acquire_token_interactively(app)
            self._auth_source = "interactive"

        self._persist_token_cache(cache)

        if "access_token" not in result:
            raise RuntimeError(
                "Authentication failed:\n"
                + json.dumps(result, indent=2)
            )

        self._access_token = result["access_token"]
        self._access_claims = self._decode_jwt_payload(self._access_token)

        self._authz_response = self._request_teams_authz(self._access_token)

        tokens = self._authz_response.get("tokens") or {}
        self._skype_token = (
            tokens.get("skypeToken")
            or tokens.get("skypetoken")
            or (self._authz_response.get("skypeToken") or {}).get("skypetoken")
        )

        if not self._skype_token:
            raise RuntimeError(
                "Teams authz succeeded but no Skype token was found.\n"
                "Response keys: "
                + ", ".join(sorted(self._authz_response.keys()))
            )

    def get_user_metadata(self) -> dict[str, Any]:
        """Return basic user metadata from the authenticated token."""
        self._require_connected()

        claims = self._access_claims

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
        """Return non-secret information about the current Teams connection."""
        self._require_connected()

        claims = self._access_claims
        expires_at = claims.get("exp")

        if isinstance(expires_at, (int, float)):
            expires_at = datetime.fromtimestamp(
                expires_at,
                tz=timezone.utc,
            ).isoformat()

        return {
            "authentication_source": self._auth_source,
            "client_id": claims.get("appid") or claims.get("azp"),
            "audience": claims.get("aud"),
            "scopes": claims.get("scp"),
            "access_token_expires_at": expires_at,
            "access_token_preview": self._token_preview(self._access_token),
            "skype_token_preview": self._token_preview(self._skype_token),
            "region": self._authz_response.get("region"),
            "region_gtms": self._authz_response.get("regionGtms"),
        }

    def list_conversations(self, limit: int = 15) -> list[dict[str, Any]]:
        """Return the latest ``limit`` conversations."""
        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        return self.get_conversations(1, limit)

    def get_conversations(
        self,
        start: int,
        end: int,
    ) -> list[dict[str, Any]]:
        """
        Return an inclusive 1-based range of conversations.

        Examples:
            get_conversations(1, 10)   -> newest 10
            get_conversations(11, 20)  -> next 10
            get_conversations(80, 120) -> positions 80 through 120

        Conversation pages are loaded lazily and retained for the lifetime of
        this connector instance. The first request asks the service for exactly
        ``end`` conversations. Later calls follow the service's backwardLink
        only when more history is needed.
        """
        self._require_connected()

        if start < 1:
            raise ValueError("start must be at least 1")
        if end < start:
            raise ValueError("end must be greater than or equal to start")

        self._ensure_conversations_loaded(end)

        selected = self._conversation_snapshot[start - 1:end]
        self._populate_conversation_display_names(selected)

        return selected

    def _ensure_conversations_loaded(self, required_count: int) -> None:
        if not self._conversation_snapshot_started:
            payload = self._fetch_conversation_page(
                url=None,
                page_size=required_count,
            )
            self._conversation_snapshot_started = True
            self._consume_conversation_page(payload)

        while (
            len(self._conversation_snapshot) < required_count
            and not self._conversation_snapshot_complete
        ):
            if not self._conversation_backward_link:
                self._conversation_snapshot_complete = True
                break

            missing = required_count - len(self._conversation_snapshot)
            previous_count = len(self._conversation_snapshot)
            previous_link = self._conversation_backward_link

            payload = self._fetch_conversation_page(
                url=previous_link,
                page_size=missing,
            )
            self._consume_conversation_page(payload)

            # Defensive stop if the private API gives us a continuation that
            # neither advances nor yields any new conversations.
            if (
                len(self._conversation_snapshot) == previous_count
                and self._conversation_backward_link == previous_link
            ):
                self._conversation_snapshot_complete = True
                break

    def _fetch_conversation_page(
        self,
        url: str | None,
        page_size: int,
    ) -> dict[str, Any]:
        if page_size <= 0:
            raise ValueError("page_size must be greater than zero")

        if url is None:
            chat_service = (self._authz_response.get("regionGtms") or {}).get(
                "chatService"
            )
            if not chat_service:
                raise RuntimeError(
                    "Teams authz response did not contain "
                    "regionGtms.chatService."
                )

            base_url = (
                f"{chat_service.rstrip('/')}/v1/users/ME/conversations"
            )
            url = self._set_query_parameters(
                base_url,
                pageSize=str(page_size),
                view="msnp24Equivalent",
            )
        else:
            # Continuation URLs already contain syncState. Preserve everything
            # returned by Teams and only tune the requested page size.
            url = self._set_query_parameters(
                url,
                pageSize=str(page_size),
            )

        return self._request_json(url)

    def _consume_conversation_page(
        self,
        payload: dict[str, Any],
    ) -> None:
        conversations = payload.get("conversations")
        if not isinstance(conversations, list):
            raise RuntimeError(
                "Unexpected conversations response. "
                f"Top-level keys: {', '.join(sorted(payload.keys()))}"
            )

        # Teams paginates conversations by "version" (last thread update),
        # not by the date of the last message. Sorting by lastMessage here can
        # put an item from an older page ahead of an item already returned.
        page = sorted(
            conversations,
            key=self._conversation_sort_key,
            reverse=True,
        )
        new_rows = [
            row for row in page
            if not row.get("id") or row["id"] not in self._conversation_ids
        ]
        if new_rows and self._conversation_snapshot:
            newest_version = self._conversation_sort_key(new_rows[0])[0]
            last_loaded_version = self._conversation_sort_key(
                self._conversation_snapshot[-1]
            )[0]
            if newest_version > last_loaded_version:
                raise RuntimeError(
                    "Teams conversation pages were not in descending version "
                    "order; the snapshot cannot be indexed reliably."
                )

        for conversation in new_rows:
            self._conversation_snapshot.append(conversation)
            conversation_id = conversation.get("id")
            if conversation_id:
                self._conversation_ids.add(conversation_id)

        metadata = payload.get("_metadata") or {}
        backward_link = metadata.get("backwardLink")

        self._conversation_backward_link = (
            backward_link
            if isinstance(backward_link, str) and backward_link
            else None
        )

        if not self._conversation_backward_link:
            self._conversation_snapshot_complete = True

    @staticmethod
    def _set_query_parameters(url: str, **updates: str) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(updates)

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )

    @classmethod
    def _conversation_sort_key(
        cls, conversation: dict[str, Any]
    ) -> tuple[int, str]:
        try:
            version = int(conversation.get("version"))
        except (TypeError, ValueError):
            version = int(cls._conversation_activity_timestamp(conversation) * 1000)
        return version, str(conversation.get("id") or "")

    def get_messages(
        self,
        conversation_id: str,
        limit: int | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve a loaded conversation's messages, oldest first.

        Pages are fetched from newest to oldest. ``limit`` and ``since`` are
        independent, inclusive constraints: retrieval ends when either is
        reached. A naive ``since`` is interpreted in the computer's local
        timezone; an aware datetime retains its stated timezone.

        Call ``get_conversations`` first so the connector can use the exact
        messages URL supplied by Teams for the selected conversation.
        """
        self._require_connected()
        if not conversation_id:
            raise ValueError("conversation_id must be non-empty")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer or None")
        if since is not None and not isinstance(since, datetime):
            raise TypeError("since must be a datetime or None")

        since_utc = since.astimezone(timezone.utc) if since else None
        conversation = next(
            (row for row in self._conversation_snapshot
             if row.get("id") == conversation_id),
            None,
        )
        if conversation is None:
            raise LookupError(
                "Conversation is not loaded. Browse it with "
                "get_conversations(start, end) first."
            )

        endpoint = conversation.get("messages")
        if not isinstance(endpoint, str) or not endpoint:
            raise RuntimeError(
                "Teams did not provide a messages URL for this conversation"
            )

        messages: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_links: set[str] = set()
        empty_pages = 0
        url = endpoint

        while url:
            self._validate_chat_service_url(url)
            if url in seen_links:
                raise RuntimeError("Teams repeated a message continuation link")
            seen_links.add(url)

            remaining = limit - len(messages) if limit is not None else 100
            page_size = min(100, remaining)
            payload = self._request_json(
                self._set_query_parameters(url, pageSize=str(page_size))
            )
            page = payload.get("messages")
            if not isinstance(page, list) or any(
                not isinstance(message, dict) for message in page
            ):
                raise RuntimeError("Unexpected Teams messages response")

            # sequenceId follows the chat's original order even after edits
            # change a message's version. Dates are used for the cutoff and
            # final chronological order.
            page.sort(key=self._message_sequence_key, reverse=True)
            crossed_date = False
            added = 0
            for message in page:
                message_id = message.get("id")
                if message_id and message_id in seen_ids:
                    continue
                timestamp = self._message_datetime(message)
                if since_utc is not None and timestamp < since_utc:
                    crossed_date = True
                    continue
                messages.append(message)
                added += 1
                if message_id:
                    seen_ids.add(message_id)
                if limit is not None and len(messages) >= limit:
                    return sorted(messages, key=self._message_chronological_key)

            if crossed_date:
                break

            empty_pages = empty_pages + 1 if added == 0 else 0
            if empty_pages >= 3:
                raise RuntimeError(
                    "Teams returned three message pages without progress"
                )
            metadata = payload.get("_metadata") or {}
            url = metadata.get("backwardLink")
            if url is not None and not isinstance(url, str):
                raise RuntimeError("Unexpected Teams message continuation link")

        return sorted(messages, key=self._message_chronological_key)

    def _validate_chat_service_url(self, url: str) -> None:
        chat_service = (self._authz_response.get("regionGtms") or {}).get(
            "chatService"
        )
        expected = urlsplit(chat_service or "")
        actual = urlsplit(url)
        if (
            actual.scheme != "https"
            or not expected.netloc
            or actual.netloc.lower() != expected.netloc.lower()
        ):
            raise RuntimeError(
                "Teams supplied a messages URL outside its chat service"
            )

    @staticmethod
    def _message_datetime(message: dict[str, Any]) -> datetime:
        for key in (
            "originalarrivaltime", "originalArrivalTime", "composetime", "composeTime"
        ):
            value = message.get(key)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.astimezone(timezone.utc)
                except ValueError:
                    continue
            if isinstance(value, (int, float)):
                seconds = value / 1000 if value > 100_000_000_000 else value
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
        raise RuntimeError("Teams message has no usable original arrival time")

    @classmethod
    def _message_sequence_key(
        cls, message: dict[str, Any]
    ) -> tuple[int, datetime, str]:
        try:
            sequence = int(message.get("sequenceId"))
        except (TypeError, ValueError):
            sequence = 0
        return sequence, cls._message_datetime(message), str(message.get("id") or "")

    @classmethod
    def _message_chronological_key(
        cls, message: dict[str, Any]
    ) -> tuple[datetime, int, str]:
        sequence, timestamp, message_id = cls._message_sequence_key(message)
        return timestamp, sequence, message_id

    def export_conversation(
        self,
        conversation_id: str,
        destination: str | Path,
        limit: int | None = None,
        since: datetime | None = None,
    ) -> Path:
        """Write a chronologically ordered UTF-8 text export.

        Each call applies the retrieval limits to this conversation alone.
        The caller chooses the destination path; existing files are replaced.
        """
        messages = self.get_messages(conversation_id, limit=limit, since=since)
        conversation = next(
            row for row in self._conversation_snapshot
            if row.get("id") == conversation_id
        )
        title = (
            conversation.get("display_name")
            or (conversation.get("threadProperties") or {}).get("topic")
            or "Teams conversation"
        )
        lines = [f"Conversation: {title}", f"Messages: {len(messages)}", ""]
        for message in messages:
            timestamp = self._message_datetime(message).isoformat()
            author = (
                message.get("imdisplayname")
                or message.get("imDisplayName")
                or message.get("fromDisplayNameInToken")
                or "Unknown sender"
            )
            content = message.get("content") or ""
            if not isinstance(content, str):
                content = str(content)
            if "html" in str(message.get("contenttype") or "").lower():
                parser = TeamsHtmlToText()
                parser.feed(content)
                content = parser.text()
            content = content.strip() or "(no text content)"
            lines.extend((f"[{timestamp}] {author}", content, ""))

        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        return path

    def _populate_conversation_display_names(
        self,
        conversations: list[dict[str, Any]],
    ) -> None:
        user_id = self._access_claims.get("oid")
        one_to_one_users: dict[str, str] = {}

        for conversation in conversations:
            if conversation.get("display_name"):
                continue

            thread = conversation.get("threadProperties") or {}

            name = (
                conversation.get("title")
                or conversation.get("displayName")
                or thread.get("topic")
                or thread.get("topicThreadTopic")
                or thread.get("spaceThreadTopic")
            )

            if name:
                conversation["display_name"] = name
                continue

            if thread.get("productThreadType") != "OneToOneChat":
                conversation["display_name"] = "(unnamed conversation)"
                continue

            other_user_id = self._other_one_to_one_user_id(
                conversation.get("id"),
                user_id,
            )

            if other_user_id:
                one_to_one_users[conversation["id"]] = other_user_id
            else:
                conversation["display_name"] = "(1:1 chat)"

        if not one_to_one_users:
            return

        profiles = self._fetch_user_profiles(
            list(dict.fromkeys(one_to_one_users.values()))
        )

        for conversation in conversations:
            if conversation.get("display_name"):
                continue

            conversation_id = conversation.get("id")
            other_user_id = one_to_one_users.get(conversation_id)

            if not other_user_id:
                continue

            profile = profiles.get(other_user_id) or {}
            name = (
                profile.get("displayName")
                or profile.get("userPrincipalName")
            )

            if not name:
                last_message = conversation.get("lastMessage") or {}
                last_sender_id = self._org_user_id_from_contact_url(
                    last_message.get("from")
                )

                if last_sender_id == other_user_id:
                    name = (
                        last_message.get("imdisplayname")
                        or last_message.get("fromDisplayNameInToken")
                    )

            conversation["display_name"] = name or f"(user {other_user_id})"

    def _fetch_user_profiles(
        self,
        user_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        normalized_ids = [user_id.lower() for user_id in user_ids]
        missing_ids = [
            user_id
            for user_id in normalized_ids
            if user_id not in self._user_profile_cache
        ]

        if missing_ids:
            middle_tier = (self._authz_response.get("regionGtms") or {}).get(
                "middleTier"
            )

            if middle_tier:
                url = (
                    f"{middle_tier.rstrip('/')}/beta/users/fetch"
                    "?isMailAddress=false"
                    "&canBeSmtpAddress=false"
                    "&enableGuest=true"
                    "&includeIBBarredUsers=true"
                    "&skypeTeamsInfo=true"
                )

                payload = self._request_json(
                    url,
                    method="POST",
                    body=[f"8:orgid:{user_id}" for user_id in missing_ids],
                )

                values = payload.get("value") or payload.get("Value") or []

                for profile in values:
                    if not isinstance(profile, dict):
                        continue

                    object_id = profile.get("objectId")
                    if object_id:
                        self._user_profile_cache[
                            str(object_id).lower()
                        ] = profile

                    mri = profile.get("mri")
                    if isinstance(mri, str) and mri.startswith("8:orgid:"):
                        self._user_profile_cache[
                            mri.removeprefix("8:orgid:").lower()
                        ] = profile

            # Cache misses too, so repeated calls do not keep asking Teams for
            # users the private endpoint cannot resolve.
            for user_id in missing_ids:
                self._user_profile_cache.setdefault(user_id, {})

        return {
            user_id: self._user_profile_cache.get(user_id, {})
            for user_id in normalized_ids
        }

    @staticmethod
    def _other_one_to_one_user_id(
        conversation_id: str | None,
        own_user_id: str | None,
    ) -> str | None:
        if not conversation_id or not own_user_id:
            return None

        prefix = "19:"
        suffix = "@unq.gbl.spaces"

        if not (
            conversation_id.startswith(prefix)
            and conversation_id.endswith(suffix)
        ):
            return None

        roster = conversation_id[len(prefix):-len(suffix)]
        parts = roster.split("_")

        own_user_id = own_user_id.lower()

        for part in parts:
            if part.lower() != own_user_id:
                return part.lower()

        return None

    @staticmethod
    def _org_user_id_from_contact_url(value: Any) -> str | None:
        if not isinstance(value, str):
            return None

        marker = "8:orgid:"
        if marker not in value:
            return None

        return value.rsplit(marker, 1)[1].lower()

    def _load_token_cache(self) -> msal.SerializableTokenCache:
        cache = msal.SerializableTokenCache()

        if not self._cache_path.exists():
            return cache

        try:
            cache.deserialize(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Could not read token cache {self._cache_path}: {exc}"
            ) from exc

        return cache

    def _persist_token_cache(
        self,
        cache: msal.SerializableTokenCache,
    ) -> None:
        if not cache.has_state_changed:
            return

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path = self._cache_path.with_suffix(
            self._cache_path.suffix + ".tmp"
        )

        try:
            temporary_path.write_text(cache.serialize(), encoding="utf-8")

            # Best effort on POSIX. Windows ACLs are left to the current user.
            if os.name != "nt":
                os.chmod(temporary_path, 0o600)

            temporary_path.replace(self._cache_path)
        except OSError as exc:
            raise RuntimeError(
                f"Could not persist token cache {self._cache_path}: {exc}"
            ) from exc
        finally:
            if temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _acquire_token_silently(
        self,
        app: msal.PublicClientApplication,
    ) -> dict[str, Any] | None:
        for account in app.get_accounts():
            result = app.acquire_token_silent(
                scopes=[self.TEAMS_SCOPE],
                account=account,
            )

            if result and "access_token" in result:
                return result

        return None

    def _acquire_token_interactively(
        self,
        app: msal.PublicClientApplication,
    ) -> dict[str, Any]:
        flow = app.initiate_device_flow(scopes=[self.TEAMS_SCOPE])

        if "user_code" not in flow:
            raise RuntimeError(
                "Could not start device-code flow:\n"
                + json.dumps(flow, indent=2)
            )

        print(flow["message"])

        return app.acquire_token_by_device_flow(flow)

    def _request_json(
        self,
        url: str,
        method: str = "GET",
        body: Any | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to a native Teams endpoint."""
        self._require_connected()

        data = None
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Authentication": f"skypetoken={self._skype_token}",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "MicrosoftTeams/1.0"
            ),
        }

        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=headers,
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Teams request failed with HTTP {exc.code} for {url}:\n"
                f"{response_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Teams request failed for {url}: {exc}"
            ) from exc

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Teams returned a non-JSON response for {url}:\n"
                + payload[:1000]
            ) from exc

    @staticmethod
    def _conversation_activity_timestamp(
        conversation: dict[str, Any],
    ) -> float:
        """Extract a sortable activity timestamp in Unix seconds."""
        last_message = conversation.get("lastMessage") or {}

        candidates = (
            last_message.get("originalarrivaltime"),
            last_message.get("originalArrivalTime"),
            last_message.get("composetime"),
            last_message.get("composeTime"),
            conversation.get("version"),
        )

        for value in candidates:
            if value is None:
                continue

            if isinstance(value, (int, float)):
                timestamp = float(value)
                return timestamp / 1000 if timestamp > 100_000_000_000 else timestamp

            if isinstance(value, str):
                stripped = value.strip()

                try:
                    timestamp = float(stripped)
                    return (
                        timestamp / 1000
                        if timestamp > 100_000_000_000
                        else timestamp
                    )
                except ValueError:
                    pass

                try:
                    return datetime.fromisoformat(
                        stripped.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    pass

        return 0.0

    def _request_teams_authz(self, access_token: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self.AUTHZ_URL,
            data=b"",
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json; ver=1.0",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Teams authz failed with HTTP {exc.code}:\n{body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Teams authz request failed: {exc}") from exc

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Teams authz returned a non-JSON response:\n"
                + payload[:1000]
            ) from exc

    def _require_connected(self) -> None:
        if not self._access_token or not self._skype_token:
            raise RuntimeError("TeamsConnector.connect() must be called first.")

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict[str, Any]:
        parts = token.split(".")
        if len(parts) < 2:
            return {}

        payload = parts[1]
        payload += "=" * (-len(payload) % 4)

        try:
            decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
            return json.loads(decoded.decode("utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _token_preview(token: str | None) -> str | None:
        if not token:
            return None

        if len(token) <= 24:
            return token[:6] + "..."

        return f"{token[:12]}...{token[-8:]}"
