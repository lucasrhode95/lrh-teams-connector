"""Product names, local data location, fixed public Teams service URLs, and file-send settings."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from teams_connector.domain.errors import ConfigurationError

APP_NAME = "lrh-teams-connector"
CLI_NAME = "teams-connector"

# Public-cloud endpoint settings. Regional Chat Service URLs still come from AuthZ.
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SEARCH_URL = "https://outlook.office.com/searchservice/api/v2/query"
# Teams Web's search-box suggestions (people), same Search token audience as SEARCH_URL.
PEOPLE_SUGGESTIONS_URL = "https://substrate.office.com/search/api/v1/suggestions?scenario=powerbar"
# Teams Web's meeting summary (the AI summary of a meeting whose transcript Teams made): the search
# recommendations route, with the feature flights Teams Web sends for it. See docs/api-research.md
# ("Meeting summary").
MEETING_SUMMARY_URL = (
    "https://substrate.office.com/search/api/v1/recommendations"
    "?setflight=AiTasksV2,AiNotesV2,AllowResultsWithoutMeeting,PeopleMentionsV2"
)
AUTHZ_URL = "https://authsvc.teams.microsoft.com/v1.0/authz"
CSA_UPDATES_URL = "https://teams.microsoft.com/api/csa/api/v1/teams/users/me/updates"

# Loop meeting notes: only these senders' messages are read for notes. Facilitator is the meeting's bot app.
# Its id is Chat Service's sender id for the app, a Microsoft first-party id (a baseline setting).
NOTES_SENDER_IDS = frozenset({"28:8e55a7b1-6766-4f0a-8610-ecacfe3d569a"})
# The media host a converted Loop page may redirect to. The region prefix varies by tenant, so this is this
# deployment's default; TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS replaces it (see notes_media_hosts).
DEFAULT_NOTES_MEDIA_HOSTS = ("francecentral1-mediap.svc.ms",)
_HOST_NAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")

# Files sent with a message go to this folder of the sender's OneDrive, as Teams Web does. Teams may
# use a localized name in other tenants.
CHAT_FILES_FOLDER = "Microsoft Teams Chat Files"
# Who may open a sent file: "chat_default" gives the chat's other members the tenant's default link
# (Teams Web's behavior; specific people, edit, when checked); "direct" grants each of them edit access
# without a link. Neither notifies anyone.
SHARED_FILE_ACCESS: Literal["chat_default", "direct"] = "chat_default"
# The largest file send_message accepts. Files of 250 MiB or more go up in a Graph upload session.
MAX_SEND_FILE_BYTES = 2 * 1024**3


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


def notes_media_hosts() -> frozenset[str]:
    """The media hosts a Loop page's converted HTML may redirect to, read on each call.

    ``TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS`` (comma-separated) replaces the default for this deployment.
    A value that is not a host name is refused, so only the hosts named here are ever followed.
    """
    raw = os.environ.get("TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS", "")
    hosts = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not hosts:
        return frozenset(DEFAULT_NOTES_MEDIA_HOSTS)
    if not all(_HOST_NAME.fullmatch(host) for host in hosts):
        raise ConfigurationError("TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS must be comma-separated host names.")
    return frozenset(hosts)
