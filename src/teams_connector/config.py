"""Product names, local data location, and fixed public Teams service URLs."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "lrh-teams-connector"
CLI_NAME = "teams-connector"

# Public-cloud endpoint settings. Regional Chat Service URLs still come from AuthZ.
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SEARCH_URL = "https://outlook.office.com/searchservice/api/v2/query"
# Teams Web's search-box suggestions (people), same Search token audience as SEARCH_URL.
PEOPLE_SUGGESTIONS_URL = "https://substrate.office.com/search/api/v1/suggestions?scenario=powerbar"
AUTHZ_URL = "https://authsvc.teams.microsoft.com/v1.0/authz"
CSA_UPDATES_URL = "https://teams.microsoft.com/api/csa/api/v1/teams/users/me/updates"


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
