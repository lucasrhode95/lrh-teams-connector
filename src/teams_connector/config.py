"""Static configuration: the Teams first-party client, its token profiles, denied pairs and local paths.

All profiles use the Microsoft Teams public client, so one device-code sign-in yields a refresh token
that silently serves every profile. The profiles encode the capability split established in
docs/teams-api-research.md (Graph where it works for this client, private Teams services elsewhere);
see docs/project-architecture.md (Porting and rechecking an environment) to adapt it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "lrh-teams-connector"
CLI_NAME = "teams-connector"
TENANT = "organizations"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"

# Public-cloud endpoint settings. Regional Chat Service URLs still come from AuthZ.
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
AUTHZ_URL = "https://authsvc.teams.microsoft.com/v1.0/authz"
CSA_UPDATES_URL = "https://teams.microsoft.com/api/csa/api/v1/teams/users/me/updates"

TEAMS_CLIENT_ID = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"


@dataclass(frozen=True)
class TokenProfile:
    """One named credential: a Microsoft first-party public client and the resource scopes it requests."""

    name: str
    client_id: str
    scopes: tuple[str, ...]
    purpose: str


PROFILES: dict[str, TokenProfile] = {
    "teams": TokenProfile(
        name="teams",
        client_id=TEAMS_CLIENT_ID,
        scopes=("https://api.spaces.skype.com/.default",),
        purpose="Teams sign-in exchange (Chat Service: chat history, members)",
    ),
    "csa": TokenProfile(
        name="csa",
        client_id=TEAMS_CLIENT_ID,
        scopes=("https://chatsvcagg.teams.microsoft.com/.default",),
        purpose="Teams chat catalog (Chat Service Aggregator)",
    ),
    "graph": TokenProfile(
        name="graph",
        client_id=TEAMS_CLIENT_ID,
        scopes=("https://graph.microsoft.com/.default",),
        purpose="Microsoft Graph: channels, people, photos, files, sending",
    ),
}

# The profile whose device-code sign-in creates the account's refresh token.
SIGN_IN_PROFILE = "teams"

# Denials observed in the tested baseline. Reassess for a new deployment; the app skips these pairs.
DENIED_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        (TEAMS_CLIENT_ID, "https://graph.microsoft.com/Chat.Read"),
    }
)


def data_dir() -> Path:
    """Per-user application data directory. ``TEAMS_CONNECTOR_HOME`` overrides it (tests, portability)."""
    override = os.environ.get("TEAMS_CONNECTOR_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Local"
    else:
        root = os.environ.get("XDG_DATA_HOME")
        base = Path(root) if root else Path.home() / ".local" / "share"
    return base / APP_NAME


def token_cache_path(*, unsecure: bool) -> Path:
    """Encrypted cache by default. The plaintext development cache is a separate, clearly named file."""
    name = "token-cache.plaintext-dev.json" if unsecure else "token-cache.bin"
    return data_dir() / name
