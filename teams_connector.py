import base64
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import msal


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
        """
        Return the most recently active Teams conversations.

        The private Teams conversations endpoint does not appear to expose a
        dependable page-size parameter, so the connector retrieves the
        conversation index, sorts it by the best available activity timestamp,
        and applies the limit locally.
        """
        self._require_connected()

        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        chat_service = (self._authz_response.get("regionGtms") or {}).get(
            "chatService"
        )
        if not chat_service:
            raise RuntimeError(
                "Teams authz response did not contain regionGtms.chatService."
            )

        payload = self._request_json(
            f"{chat_service.rstrip('/')}/v1/users/ME/conversations"
        )

        conversations = payload.get("conversations")
        if not isinstance(conversations, list):
            raise RuntimeError(
                "Unexpected conversations response. "
                f"Top-level keys: {', '.join(sorted(payload.keys()))}"
            )

        conversations.sort(
            key=self._conversation_activity_timestamp,
            reverse=True,
        )

        return conversations[:limit]

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

    def _request_json(self, url: str) -> dict[str, Any]:
        """Make an authenticated request to the native Teams messaging API."""
        self._require_connected()

        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Authentication": f"skypetoken={self._skype_token}",
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "MicrosoftTeams/1.0"
                ),
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Teams request failed with HTTP {exc.code} for {url}:\n{body}"
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
        """
        Extract a sortable activity timestamp from a native conversation object.

        Teams has used a few representations here over time, so prefer the
        last-message timestamps and fall back to the conversation version.
        """
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
                return float(value)

            if isinstance(value, str):
                stripped = value.strip()

                try:
                    return float(stripped)
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


if __name__ == "__main__":
    connector = TeamsConnector()
    connector.connect()

    print("\nConnected.")

    conversations = connector.list_conversations(limit=15)

    print(f"\nLatest {len(conversations)} conversations:")

    for number, conversation in enumerate(conversations, start=1):
        last_message = conversation.get("lastMessage") or {}
        properties = conversation.get("properties") or {}

        title = (
            conversation.get("title")
            or conversation.get("displayName")
            or properties.get("topic")
            or properties.get("subject")
            or "(no title)"
        )

        last_sender = (
            last_message.get("imdisplayname")
            or last_message.get("imDisplayName")
            or last_message.get("from")
            or "unknown"
        )

        last_time = (
            last_message.get("originalarrivaltime")
            or last_message.get("originalArrivalTime")
            or last_message.get("composetime")
            or last_message.get("composeTime")
            or conversation.get("version")
            or "unknown"
        )

        print(
            f"{number:>2}. {title}\n"
            f"    id: {conversation.get('id')}\n"
            f"    last sender: {last_sender}\n"
            f"    last activity: {last_time}"
        )
