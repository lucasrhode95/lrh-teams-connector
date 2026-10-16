"""Composition root: config → tokens → transport → adapters → store → services.

Built lazily on first use, so a starting process does no network or disk work (architecture,
"Requests, caching, and storage"). This is the one place that decides which backend serves each
capability (requirements §4).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from m365_auth import Account, AccountBoundTokenSource, AuthenticationRequired, TokenProvider

from teams_connector import config
from teams_connector.domain.models import Participant
from teams_connector.remote.chat_service import ChatService
from teams_connector.remote.csa import CsaCatalog
from teams_connector.remote.csa_mapping import Me
from teams_connector.remote.graph import Graph
from teams_connector.remote.graph_teams import (
    GraphChannels,
    GraphFiles,
    GraphFileSharer,
    GraphPeople,
    GraphWriter,
)
from teams_connector.remote.graph_transcripts import GraphTranscripts, GraphVideoSearch
from teams_connector.remote.image_readers import AmsCredential, AmsImages, GraphHostedImages
from teams_connector.remote.presence import TeamsPresence
from teams_connector.remote.substrate_search import SubstrateChatSearch, SubstrateSearch
from teams_connector.remote.substrate_summaries import SubstrateMeetingSummaries
from teams_connector.remote.teams_session import TeamsSession
from teams_connector.remote.transport import Bearer, Transport
from teams_connector.service.catalog import Catalog
from teams_connector.service.delete import Deleter
from teams_connector.service.export.orchestrator import Exports
from teams_connector.service.files import Files, Photos
from teams_connector.service.history import History
from teams_connector.service.presence import Presences
from teams_connector.service.search import Search
from teams_connector.service.send import Sender
from teams_connector.service.summaries import MeetingSummaries
from teams_connector.service.transcripts import TranscriptSearch
from teams_connector.store.db import Store, store_path


@dataclass
class Services:
    account: Account
    me: Participant
    store: Store
    catalog: Catalog
    history: History
    search: Search
    exports: Exports
    sender: Sender
    deleter: Deleter
    files: Files
    photos: Photos
    presence: Presences
    transcript_search: TranscriptSearch


class AppContext:
    def __init__(
        self,
        *,
        unsecure: bool = False,
        tokens: TokenProvider | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.tokens = tokens or TokenProvider(unsecure=unsecure, login_command=f"{config.CLI_NAME} auth")
        self._http_client = http_client
        self._transport: Transport | None = None
        self._services: Services | None = None
        self._lock = asyncio.Lock()

    async def services(self) -> Services:
        account, claims = self._current_identity()
        if self._services is not None and self._services.account.fingerprint == account.fingerprint:
            return self._services
        async with self._lock:
            account, claims = self._current_identity()
            if self._services is None or self._services.account.fingerprint != account.fingerprint:
                self._services = await self._build(account, claims)
        return self._services

    def _current_identity(self) -> tuple[Account, dict[str, Any]]:
        claims = self.tokens.get_token("teams.spaces").claims()
        if not claims.get("tid") or not claims.get("oid"):
            raise AuthenticationRequired(
                "The Teams token carries no account identity.", command=self.tokens.sign_in_command()
            )
        account = Account(
            tenant_id=str(claims["tid"]),
            object_id=str(claims["oid"]).lower(),
            username=claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name"),
        )
        return account, claims

    async def _build(self, account: Account, claims: dict[str, Any]) -> Services:
        name = claims.get("name") if isinstance(claims.get("name"), str) else account.username
        me = Me(user_id=account.object_id, name=name)
        account_tokens = AccountBoundTokenSource(self.tokens, account)
        if self._transport is None:
            self._transport = Transport(client=self._http_client)
        transport = self._transport
        graph = Graph(transport, Bearer(account_tokens, "teams.graph"))
        session = TeamsSession(transport, account_tokens)
        people = GraphPeople(graph)
        channels = GraphChannels(graph)
        chat_service = ChatService(transport, session, people)
        catalog_source = CsaCatalog(transport, session, account_tokens, people, chat_service, me)

        store = Store(store_path(account.fingerprint), account.fingerprint)
        member_search = SubstrateChatSearch(
            transport,
            Bearer(account_tokens, "m365.search"),
            anchor_mailbox=f"Oid:{account.object_id}@{account.tenant_id}",
            me_id=account.object_id,
        )
        catalog = Catalog(
            store,
            catalog_source,
            channels,
            chat_service,
            members=member_search,
        )
        summaries = MeetingSummaries(
            SubstrateMeetingSummaries(
                transport,
                Bearer(account_tokens, "m365.search"),
                anchor_mailbox=f"Oid:{account.object_id}@{account.tenant_id}",
            )
        )
        history = History(catalog, chat_service, channels, account.object_id, summaries)
        search_source = SubstrateSearch(
            transport,
            Bearer(account_tokens, "m365.search"),
            anchor_mailbox=f"Oid:{account.object_id}@{account.tenant_id}",
        )
        shared_files = GraphFiles(graph)
        transcripts = GraphTranscripts(graph)
        files = Files(
            history,
            shared_files,
            AmsImages(transport, AmsCredential(session)),
            GraphHostedImages(graph),
            shared_files,
            transcripts,
        )
        person = Participant(id=account.object_id, name=name)
        return Services(
            account=account,
            me=person,
            store=store,
            catalog=catalog,
            history=history,
            search=Search(store, search_source),
            exports=Exports(catalog, history, files, summaries),
            sender=Sender(
                catalog,
                GraphWriter(graph),
                sharer=GraphFileSharer(
                    graph, folder=config.CHAT_FILES_FOLDER, access=config.SHARED_FILE_ACCESS
                ),
                members=chat_service,
                me_id=account.object_id,
            ),
            deleter=Deleter(catalog, history, chat_service),
            files=files,
            photos=Photos(people),
            transcript_search=TranscriptSearch(
                catalog, history, transcripts, GraphVideoSearch(graph), summaries
            ),
            presence=Presences(
                catalog, TeamsPresence(transport, session, Bearer(account_tokens, "teams.spaces"))
            ),
        )

    async def aclose(self) -> None:
        if self._transport is not None:
            await self._transport.aclose()
