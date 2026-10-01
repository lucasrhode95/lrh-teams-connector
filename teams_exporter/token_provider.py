"""Shared MSAL token access and cache persistence for Teams clients.

Encrypted OS persistence is the default. ``unsecure=True`` explicitly selects
an unencrypted development cache in the repository's ignored ``.local`` folder.
Never log, return to UI output, or commit that cache.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import msal


logger = logging.getLogger(__name__)


class AuthenticationRequiredError(RuntimeError):
    """The app-owned Microsoft session needs an interactive sign-in."""


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A token plus its acquisition source; token text is hidden from repr."""

    value: str = field(repr=False)
    source: str


class _TokenCacheLock:
    """Cross-process lock shared by all users of one selected cache file."""

    def __init__(self, path: Path, timeout_seconds: float = 30.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        if self._stream.seek(0, os.SEEK_END) == 0:
            self._stream.write(b"0")
            self._stream.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._stream.seek(0)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise RuntimeError(
                        "The Teams token cache is busy in another process. Retry shortly."
                    ) from None
                time.sleep(0.1)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()


class TokenProvider:
    """One configured MSAL token surface shared by connector consumers.

    The secure cache is selected by default. A separate plaintext development
    cache can be selected explicitly for an agent or local debugging process.
    """

    DEFAULT_CLIENT_ID = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"
    _REJECTED_CREDENTIAL_ERRORS = {
        "invalid_grant",
        "interaction_required",
        "login_required",
        "consent_required",
        "access_denied",
        "expired_token",
    }
    _TRANSIENT_TOKEN_ERRORS = {
        "temporarily_unavailable",
        "server_error",
        "service_not_available",
        "request_timeout",
        "timeout",
    }

    def __init__(
        self,
        tenant: str = "organizations",
        client_id: str = DEFAULT_CLIENT_ID,
        cache_path: str | Path | None = None,
        unsecure: bool = False,
    ) -> None:
        self._authority = f"https://login.microsoftonline.com/{tenant}"
        self._client_id = client_id
        self.unsecure = bool(unsecure)
        selected_path = cache_path
        if selected_path is None:
            selected_path = (
                self._default_debug_cache_path()
                if self.unsecure
                else self._default_secure_cache_path()
            )
        self.cache_path = Path(selected_path).expanduser().resolve()
        self._cache_lock_path = self.cache_path.with_suffix(
            self.cache_path.suffix + ".lock"
        )

    @property
    def mode(self) -> str:
        return "plaintext-development" if self.unsecure else "encrypted"

    @property
    def sign_in_command(self) -> str:
        suffix = " --unsecure" if self.unsecure else ""
        return f"python -m teams_exporter auth{suffix}"

    @staticmethod
    def unsecure_warning(path: str | Path) -> str:
        return (
            "WARNING: the development MSAL cache stores reusable Microsoft access "
            "and refresh tokens as plaintext at "
            f"{Path(path)}. Any process that can read this file can use the session. "
            "Do not share or commit it; delete it when development is finished."
        )

    def get_access_tokens(
        self,
        scopes: Iterable[str],
        interactive: bool = False,
    ) -> list[AccessToken]:
        """Acquire multiple resource tokens under one cache lock."""
        requested_scopes = tuple(scopes)
        if not requested_scopes:
            return []
        logger.debug(
            "MSAL token acquisition started mode=%s scope_count=%d interactive=%s",
            self.mode,
            len(requested_scopes),
            interactive,
        )
        with _TokenCacheLock(self._cache_lock_path):
            cache = self.load_cache()
            app = msal.PublicClientApplication(
                client_id=self._client_id,
                authority=self._authority,
                token_cache=cache,
            )
            results = [
                self._acquire_resource_token(app, scope, interactive=interactive)
                for scope in requested_scopes
            ]
        return [AccessToken(value=result["access_token"], source=source) for result, source in results]

    def get_cached_account(self) -> dict[str, Any] | None:
        """Return the first cached MSAL account without making network requests."""
        with _TokenCacheLock(self._cache_lock_path):
            cache = self.load_cache()
        accounts = list(cache.search(msal.TokenCache.CredentialType.ACCOUNT))
        return accounts[0] if accounts else None

    def load_cache(self) -> Any:
        """Load the selected MSAL cache using its configured persistence mode."""
        return self._new_persisted_token_cache(self.cache_path)

    def _acquire_resource_token(
        self,
        app: msal.PublicClientApplication,
        scope: str,
        interactive: bool,
    ) -> tuple[dict[str, Any], str]:
        for account in app.get_accounts():
            result = self._retry_token_request(
                lambda: app.acquire_token_silent(scopes=[scope], account=account),
                scope=scope,
            )
            if isinstance(result, dict) and result.get("access_token"):
                return result, "cache"

        if not interactive:
            raise AuthenticationRequiredError(
                "Teams sign-in is required. Run "
                f"`{self.sign_in_command}` in a terminal, complete Microsoft sign-in, then retry."
            )

        flow = app.initiate_device_flow(scopes=[scope])
        if not isinstance(flow, dict) or not flow.get("user_code"):
            raise RuntimeError(
                f"Could not start device-code authentication for {self._scope_name(scope)}."
            )
        print(flow.get("message", "Complete sign-in in the browser window."))
        result = self._retry_token_request(
            lambda: app.acquire_token_by_device_flow(flow),
            scope=scope,
            interactive=True,
        )
        if not result or not result.get("access_token"):
            self._raise_token_result_error(result, scope=scope)
        return result, "device_code"

    def _retry_token_request(
        self,
        operation: Callable[[], Any],
        scope: str,
        interactive: bool = False,
    ) -> Any:
        attempts = 3
        for attempt in range(attempts):
            try:
                result = operation()
            except Exception as exc:
                if attempt + 1 >= attempts:
                    kind = "connectivity" if self._is_network_error(exc) else "service"
                    raise RuntimeError(
                        f"Microsoft {kind} request failed after {attempts} attempts. "
                        "The stored credentials were not reported as rejected; try again later."
                    ) from None
                time.sleep(min(0.25 * (2**attempt), 1.0))
                continue

            if isinstance(result, dict) and result.get("access_token"):
                return result
            error = str(result.get("error", "") if isinstance(result, dict) else "").casefold()
            if error in self._REJECTED_CREDENTIAL_ERRORS:
                raise self._stored_credentials_rejected() from None
            if attempt + 1 >= attempts:
                if error:
                    kind = "connectivity" if error in self._TRANSIENT_TOKEN_ERRORS else "service"
                    raise RuntimeError(
                        f"Microsoft {kind} request failed after {attempts} attempts. "
                        "The stored credentials were not reported as rejected; try again later."
                    ) from None
                if interactive:
                    self._raise_token_result_error(result, scope=scope)
                return result
            time.sleep(min(0.25 * (2**attempt), 1.0))
        return None

    @staticmethod
    def _is_network_error(exc: Exception) -> bool:
        return isinstance(exc, (OSError, TimeoutError, urllib.error.URLError)) or (
            exc.__class__.__module__.startswith(("requests", "httpx"))
        )

    def _stored_credentials_rejected(self) -> AuthenticationRequiredError:
        return AuthenticationRequiredError(
            "Microsoft no longer accepts the stored credentials. "
            f"Run `{self.sign_in_command}` to sign in again, then retry."
        )

    def _raise_token_result_error(self, result: Any, *, scope: str) -> None:
        error = str(result.get("error", "") if isinstance(result, dict) else "").casefold()
        if error in self._REJECTED_CREDENTIAL_ERRORS:
            raise self._stored_credentials_rejected()
        raise RuntimeError(
            f"Microsoft sign-in failed for {self._scope_name(scope)}. "
            f"Run `{self.sign_in_command}` and retry."
        )

    @staticmethod
    def _scope_name(scope: str) -> str:
        return "CSA" if "chatsvcagg" in scope else "Teams"

    def _new_persisted_token_cache(self, path: Path) -> Any:
        """Create a cache using the selected explicit persistence mode."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from msal_extensions import FilePersistence, PersistedTokenCache

            persistence = (
                FilePersistence(str(path))
                if self.unsecure
                else self._encrypted_persistence(path)
            )
            return PersistedTokenCache(persistence)
        except Exception:
            if self.unsecure:
                raise RuntimeError(
                    "The plaintext development MSAL cache could not be opened."
                ) from None
            raise self._encrypted_storage_unavailable() from None

    @staticmethod
    def _encrypted_persistence(path: Path) -> Any:
        from msal_extensions import build_encrypted_persistence

        return build_encrypted_persistence(str(path))

    @staticmethod
    def _encrypted_storage_unavailable() -> RuntimeError:
        return RuntimeError(
            "Encrypted Teams credential storage is unavailable. No plaintext cache "
            "will be used. You may need to sign in again after encrypted storage is available."
        )

    @staticmethod
    def _default_secure_cache_path() -> Path:
        if os.name == "nt":
            root = os.environ.get("LOCALAPPDATA")
            base = Path(root) if root else Path.home() / "AppData" / "Local"
        else:
            root = os.environ.get("XDG_DATA_HOME")
            base = Path(root) if root else Path.home() / ".local" / "share"
        return base / "scrape-teams-chat" / ".teams_token_cache.json"

    @staticmethod
    def _default_debug_cache_path() -> Path:
        repository = Path(__file__).resolve().parent.parent
        return repository / ".local" / "teams-msal-debug-cache.json"
